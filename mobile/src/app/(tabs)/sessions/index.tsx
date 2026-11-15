// Sessions tab (#78): the session library (pinned, recently focused, new and
// recent) and the connected OpenCode workers, like the web's Sessions and
// Connected OpenCodes sections.
import { useState } from "react";
import { Pressable, View } from "react-native";
import { Stack } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import type { Session, Worker } from "@/api/types";
import { health, usable, useRecentSessions, useWorkers } from "@/sessions/api";
import { sessionIdentity, useSessionLibrary } from "@/sessions/library";
import { openSession, openWorker } from "@/sessions/nav";
import type { SessionReference } from "@/sessions/library";
import {
  Button,
  Choice,
  Empty,
  ErrorState,
  Heading,
  Label,
  LiveStatus,
  Loading,
  Panel,
  Row,
  Screen,
  usePullRefresh,
} from "@/ui/kit";
import { age } from "@/ui/format";

function Pager({
  offset,
  count,
  size,
  change,
}: {
  offset: number;
  count: number;
  size: number;
  change: (offset: number) => void;
}) {
  if (count <= size) return null;
  return (
    <Row style={{ justifyContent: "space-between" }}>
      <Button
        small
        label="Previous"
        disabled={!offset}
        onPress={() => change(Math.max(0, offset - size))}
      />
      <Label tone="muted" size={13}>
        {offset + 1}–{Math.min(offset + size, count)} of {count}
      </Label>
      <Button
        small
        label="Next"
        disabled={offset + size >= count}
        onPress={() => change(offset + size)}
      />
    </Row>
  );
}

function ShelfItem({ item, current }: { item: SessionReference; current?: Session }) {
  const library = useSessionLibrary();
  const pinned = library.isPinned(item);
  const title = current?.title || item.title || item.sessionID;
  return (
    <Panel accessibilityLabel={`Open session ${title}`} onPress={() => openSession(item)}>
      <Row style={{ justifyContent: "space-between" }} wrap={false}>
        <View style={{ flex: 1, gap: 2 }}>
          <Label bold numberOfLines={2}>
            {title}
          </Label>
          <Label tone="muted" size={12}>
            {item.worker}
            {current?.executionStatus ? ` · ${current.executionStatus}` : ""}
            {current?.counts.pending ? ` · ${current.counts.pending} needs input` : ""}
          </Label>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={pinned ? `Unpin ${title}` : `Pin ${title}`}
          hitSlop={10}
          onPress={() =>
            library.togglePin({ worker: item.worker, sessionID: item.sessionID, title })
          }
        >
          <Label tone={pinned ? "accent" : "muted"} size={18}>
            {pinned ? "★" : "☆"}
          </Label>
        </Pressable>
      </Row>
    </Panel>
  );
}

function Library() {
  const [offset, setOffset] = useState(0);
  const library = useSessionLibrary();
  const recent = useRecentSessions(offset);
  const saved = new Set(library.entries.map(sessionIdentity));
  const lookup = new Map((recent.data?.items ?? []).map((item) => [sessionIdentity(item), item]));
  const groups: { title: string; items: SessionReference[]; empty: string }[] = [
    {
      title: "Pinned",
      items: library.entries.filter((item) => item.pinned),
      empty: "Pin a conversation to keep it here.",
    },
    {
      title: "Recently focused",
      items: library.entries.filter((item) => !item.pinned),
      empty: "No sessions yet.",
    },
    {
      title: "New and recent",
      items: (recent.data?.items ?? []).filter((item) => !saved.has(sessionIdentity(item))),
      empty: "No sessions yet.",
    },
  ];
  return (
    <>
      {groups.map((group) => (
        <View key={group.title} style={{ gap: 8 }}>
          <Label tone="muted" size={12} bold>
            {group.title.toUpperCase()}
          </Label>
          {group.items.length ? (
            group.items.map((item) => (
              <ShelfItem
                key={sessionIdentity(item)}
                item={item}
                current={lookup.get(sessionIdentity(item))}
              />
            ))
          ) : (
            <Label tone="muted" size={13}>
              {group.empty}
            </Label>
          )}
        </View>
      ))}
      {recent.error ? (
        <ErrorState error={recent.error} retry={() => void recent.refetch()} />
      ) : null}
      <Pager offset={offset} count={recent.data?.count ?? 0} size={20} change={setOffset} />
    </>
  );
}

function WorkerRow({ worker }: { worker: Worker }) {
  return (
    <Panel
      accessibilityLabel={`Open worker ${worker.name}`}
      onPress={() => openWorker(worker.name)}
    >
      <Row style={{ justifyContent: "space-between" }}>
        <Label size={16} bold>
          {worker.name}
        </Label>
        <Label tone={usable(worker) ? "accent" : "danger"} size={13} bold>
          {health(worker)}
        </Label>
      </Row>
      <Label tone="secondary" size={13}>
        {worker.hostname || "Waiting for first sync"}
      </Label>
      <Label tone="muted" size={12}>
        Seen {age(worker.last_sync_at)}
        {worker.counts
          ? ` · ${worker.counts.sessions} sessions · ${worker.counts.pending} waiting`
          : ""}
      </Label>
    </Panel>
  );
}

function Workers() {
  const [offset, setOffset] = useState(0);
  const workers = useWorkers(offset);
  if (workers.isLoading) return <Loading label="Loading workers" />;
  if (workers.error)
    return <ErrorState error={workers.error} retry={() => void workers.refetch()} />;
  if (!workers.data?.count)
    return (
      <Empty
        title="No OpenCodes registered"
        detail="Provision a worker and run its outbound bridge with this coordinator URL and its bound agent key."
      />
    );
  return (
    <>
      {workers.data.items.map((worker) => (
        <WorkerRow key={worker.name} worker={worker} />
      ))}
      <Pager offset={offset} count={workers.data.count} size={50} change={setOffset} />
    </>
  );
}

export default function Sessions() {
  const [mode, setMode] = useState<"library" | "workers">("library");
  const workers = useWorkers();
  const client = useQueryClient();
  const pull = usePullRefresh(() =>
    Promise.all([
      client.invalidateQueries({ queryKey: ["workers"] }),
      client.invalidateQueries({ queryKey: ["sessions"] }),
    ]),
  );
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: "Sessions" }} />
      <Heading eyebrow="Central worker console">
        {mode === "library" ? "Sessions" : "Connected OpenCodes"}
      </Heading>
      <Label tone="secondary" size={13}>
        {mode === "library"
          ? "Pinned, recently opened and new conversations. Open a worker to start a new one."
          : "Live sessions from your machines. Coordinated here."}
      </Label>
      <Row style={{ justifyContent: "space-between" }}>
        <Choice
          value={mode}
          options={[
            { value: "library", label: "Library" },
            { value: "workers", label: `Workers${workers.data ? ` ${workers.data.count}` : ""}` },
          ]}
          onChange={setMode}
        />
        <LiveStatus />
      </Row>
      {mode === "library" ? <Library /> : <Workers />}
    </Screen>
  );
}
