// Multi-AI chat rooms on mobile (GH #149), mirroring frontend/src/ChatRooms.tsx.
// Read the transcript, post, and advance one speaker at a time. Room writes are
// owner-only on the server, so the controls are gated the same way here.
import { useCallback, useState } from "react";
import { Stack } from "expo-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import {
  Badge,
  Button,
  Empty,
  ErrorState,
  Field,
  Heading,
  Label,
  Loading,
  Notice,
  Panel,
  Row,
  Screen,
  usePullRefresh,
} from "@/ui/kit";
import { errorText } from "@/ui/format";

interface Participant {
  id: number;
  position: number;
  role: string;
  canAct: boolean;
  isMuted: boolean;
  model: { providerID: string; modelID: string } | null;
}

interface Room {
  id: number;
  project: string;
  name: string;
  topic: string;
  status: "open" | "closed";
  messageCount: number;
  pendingSpeakers: number[];
  participants?: Participant[];
}

interface Message {
  id: number;
  seq: number;
  authorKind: "human" | "participant" | "system";
  role: string;
  text: string;
  toolEvents: { tool: string; is_error: boolean }[];
}

export default function ChatRooms() {
  const { api } = useSession();
  const { canManage } = useAccess();
  const client = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const rooms = useQuery({
    queryKey: ["chatrooms", "list"],
    enabled: !!api,
    refetchInterval: 10_000,
    queryFn: async () =>
      (await unwrap(api!.GET("/api/v1/chat/rooms"))) as unknown as {
        count: number;
        items: Room[];
      },
  });
  const room = useQuery({
    queryKey: ["chatrooms", "room", selected],
    enabled: !!api && selected !== null,
    refetchInterval: 5_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/chat/rooms/{id}", { params: { path: { id: selected! } } }),
      )) as unknown as Room,
  });
  const history = useQuery({
    queryKey: ["chatrooms", "history", selected],
    enabled: !!api && selected !== null,
    refetchInterval: 5_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/chat/rooms/{id}/messages", {
          params: { path: { id: selected! }, query: { latest: true, limit: 20 } },
        }),
      )) as unknown as { messages: Message[]; messageCount: number },
  });

  const refresh = useCallback(() => {
    void client.invalidateQueries({ queryKey: ["chatrooms"] });
  }, [client]);

  // One request per speaker, as on the web: the lease and the pending queue live
  // on the server, so stopping early is safe and the turn resumes later.
  const runTurn = useCallback(
    async (id: number) => {
      for (let guard = 0; guard < 24; guard += 1) {
        const result = (await unwrap(
          api!.POST("/api/v1/chat/rooms/{id}/advance", {
            params: { path: { id } },
            body: {},
          }),
        )) as unknown as { advanced: boolean; room: Room };
        refresh();
        if (!result.advanced || result.room.pendingSpeakers.length === 0) break;
      }
    },
    [api, refresh],
  );

  async function send() {
    if (selected === null || !draft.trim()) return;
    const text = draft;
    setBusy(true);
    setError("");
    setDraft("");
    try {
      await unwrap(
        api!.POST("/api/v1/chat/rooms/{id}/messages", {
          params: { path: { id: selected } },
          body: { text },
        }),
      );
      refresh();
      await runTurn(selected);
    } catch (problem) {
      setError(errorText(problem));
      setDraft(text);
    } finally {
      setBusy(false);
      refresh();
    }
  }

  const listRefresh = usePullRefresh(() => rooms.refetch());
  const roomRefresh = usePullRefresh(() => Promise.all([room.refetch(), history.refetch()]));
  const current = room.data;
  const pending = current?.pendingSpeakers ?? [];
  const nextSpeaker = current?.participants?.find((item) => item.id === pending[0]);

  if (selected === null)
    return (
      <Screen {...listRefresh}>
        <Stack.Screen options={{ title: "Chat rooms" }} />
        <Heading eyebrow="MULTI-AGENT">Chat rooms</Heading>
        {rooms.isLoading ? (
          <Loading label="Loading rooms…" />
        ) : rooms.error ? (
          <ErrorState error={rooms.error} retry={() => void rooms.refetch()} />
        ) : !rooms.data?.count ? (
          <Empty title="No rooms yet" detail="Create a room in the web app." />
        ) : (
          rooms.data.items.map((item) => (
            <Panel
              key={item.id}
              accessibilityLabel={item.name}
              onPress={() => setSelected(item.id)}
            >
              <Label size={16} bold>
                {item.name}
              </Label>
              <Label tone="secondary" size={13}>
                {item.project} · {item.messageCount} messages
                {item.status === "closed" ? " · closed" : ""}
              </Label>
            </Panel>
          ))
        )}
      </Screen>
    );

  return (
    <Screen {...roomRefresh}>
      <Stack.Screen options={{ title: current?.name ?? "Room" }} />
      <Button label="← All rooms" kind="secondary" onPress={() => setSelected(null)} />
      {current && (
        <Panel>
          <Label size={16} bold>
            {current.name}
          </Label>
          <Label tone="secondary" size={13}>
            {current.topic || "No topic set."}
          </Label>
          <Row>
            {(current.participants ?? []).map((participant) => (
              <Badge
                key={participant.id}
                status={participant.canAct ? "done" : "pending"}
                label={`${participant.role}${participant.canAct ? " · acts" : ""}${
                  participant.isMuted ? " · muted" : ""
                }`}
              />
            ))}
          </Row>
        </Panel>
      )}
      {history.isLoading ? (
        <Loading label="Loading transcript…" />
      ) : history.error ? (
        <ErrorState error={history.error} retry={() => void history.refetch()} />
      ) : !history.data?.messages.length ? (
        <Empty title="Nothing said yet" />
      ) : (
        history.data.messages.map((message) => (
          <Panel key={message.id}>
            <Label size={13} bold>
              {message.authorKind === "participant"
                ? message.role
                : message.authorKind === "system"
                  ? "System"
                  : message.role || "You"}
            </Label>
            <Label>{message.text}</Label>
            {message.toolEvents.length > 0 && (
              <Label tone="muted" size={12}>
                {message.toolEvents
                  .map((event) => `${event.tool}${event.is_error ? " failed" : ""}`)
                  .join(" · ")}
              </Label>
            )}
          </Panel>
        ))
      )}
      {pending.length > 0 && (
        <Label tone="secondary" size={13}>
          {nextSpeaker ? `${nextSpeaker.role} is thinking…` : "Turn in progress…"} ·{" "}
          {pending.length} to speak
        </Label>
      )}
      {error ? <Notice error>{error}</Notice> : null}
      {canManage && current?.status === "open" && (
        <Panel>
          <Field
            label="Message"
            value={draft}
            onChangeText={setDraft}
            multiline
            placeholder="Ask the room something…"
          />
          <Button
            label={busy ? "Sending…" : "Send"}
            onPress={() => void send()}
            disabled={busy || !draft.trim()}
          />
          {pending.length > 0 && (
            <Button
              label="Continue the turn"
              kind="secondary"
              onPress={() => void runTurn(current.id)}
            />
          )}
        </Panel>
      )}
      {!canManage && (
        <Label tone="muted" size={13}>
          Only an owner can post into a room.
        </Label>
      )}
    </Screen>
  );
}
