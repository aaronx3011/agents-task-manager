// Activity tab (#80): recent worker commands and task activity, like the web
// Activity page.
import { useState } from "react";
import { router, Stack } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type { Page, RemoteCommand } from "@/api/types";
import { useSession } from "@/auth/session";
import { openSession } from "@/sessions/nav";
import {
  Button,
  Empty,
  ErrorState,
  Heading,
  Label,
  LiveStatus,
  Loading,
  Notice,
  Panel,
  Row,
  Screen,
  usePullRefresh,
} from "@/ui/kit";
import { age, words } from "@/ui/format";

const SIZE = 30;

function commandState(command: RemoteCommand) {
  if (command.deliveryUncertain) return "Delivery uncertain";
  if (command.status === "done" && command.action === "prompt")
    return "Accepted · execution separate";
  return command.status;
}

export default function Activity() {
  const { api } = useSession();
  const [offset, setOffset] = useState(0);
  const commands = useQuery({
    queryKey: ["activity", "commands", offset],
    enabled: !!api,
    refetchInterval: 10_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/commands", { params: { query: { limit: SIZE, offset } } }),
      )) as unknown as Page<RemoteCommand>,
  });
  const workspace = useQuery({
    queryKey: ["workspace", "activity"],
    enabled: !!api,
    queryFn: () => unwrap(api!.GET("/api/v1/dashboard/workspace", { params: { query: {} } })),
  });
  const pull = usePullRefresh(() => Promise.all([commands.refetch(), workspace.refetch()]));
  const events = workspace.data?.events ?? [];
  const count = commands.data?.count ?? 0;
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: "Activity" }} />
      <Heading eyebrow="Workspace timeline">Activity</Heading>
      <Label tone="secondary" size={13}>
        Worker commands and task updates, separate from your conversations.
      </Label>
      <LiveStatus />
      <Label tone="muted" size={12} bold>
        RECENT WORKER COMMANDS{count ? ` · ${count}` : ""}
      </Label>
      {commands.isLoading ? (
        <Loading label="Loading commands" />
      ) : commands.error ? (
        <ErrorState error={commands.error} retry={() => void commands.refetch()} />
      ) : !count ? (
        <Empty title="No worker commands yet" />
      ) : (
        commands.data!.items.map((command) => (
          <Panel key={command.id}>
            <Row style={{ justifyContent: "space-between" }}>
              <Label bold>
                #{command.id} · {command.action}
              </Label>
              <Label tone="muted" size={12}>
                {age(command.createdAt)}
              </Label>
            </Row>
            <Label
              tone={
                command.deliveryUncertain || command.status === "error" ? "danger" : "secondary"
              }
              size={13}
            >
              {command.worker} · {commandState(command)}
            </Label>
            {command.error ? <Notice error>{command.error}</Notice> : null}
            {command.deliveryUncertain ? (
              <Notice>
                {command.uncertain ||
                  "Inspect session history and reconcile this command through the staff API. It will not be replayed automatically."}
              </Notice>
            ) : null}
            {command.sessionID ? (
              <Button
                small
                label="Open session"
                onPress={() =>
                  openSession({ worker: command.worker, sessionID: command.sessionID! })
                }
              />
            ) : null}
          </Panel>
        ))
      )}
      {count > SIZE ? (
        <Row style={{ justifyContent: "space-between" }}>
          <Button
            small
            label="Previous"
            disabled={!offset}
            onPress={() => setOffset(Math.max(0, offset - SIZE))}
          />
          <Label tone="muted" size={13}>
            {offset + 1}–{Math.min(offset + SIZE, count)} of {count}
          </Label>
          <Button
            small
            label="Next"
            disabled={offset + SIZE >= count}
            onPress={() => setOffset(offset + SIZE)}
          />
        </Row>
      ) : null}
      <Label tone="muted" size={12} bold>
        TASK ACTIVITY
      </Label>
      {workspace.error ? (
        <ErrorState error={workspace.error} retry={() => void workspace.refetch()} />
      ) : null}
      {events.length ? (
        events.map((event) => (
          <Panel
            key={event.id}
            accessibilityLabel={`Open task ${event.task_id}: ${event.title}`}
            onPress={() => router.push({ pathname: "/tasks/[id]", params: { id: event.task_id } })}
          >
            <Row style={{ justifyContent: "space-between" }}>
              <Label bold size={14}>
                {event.agent || "System / human"} · {words(event.event_type)}
              </Label>
              <Label tone="muted" size={12}>
                {age(event.created_at)}
              </Label>
            </Row>
            <Label tone="secondary">
              #{event.task_id} {event.title}
            </Label>
            {event.message ? (
              <Label tone="muted" size={13} numberOfLines={3}>
                {event.message}
              </Label>
            ) : null}
          </Panel>
        ))
      ) : workspace.isLoading ? (
        <Loading label="Loading activity" />
      ) : (
        <Empty title="No task activity yet" />
      )}
    </Screen>
  );
}
