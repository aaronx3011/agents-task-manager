// Task detail (#77): the web TaskModal's overview and activity timeline.
import { useState } from "react";
import { View } from "react-native";
import { router, Stack, useLocalSearchParams } from "expo-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type { Schemas } from "@/api/client";
import { useSession } from "@/auth/session";
import {
  Avatar,
  Badge,
  Button,
  Choice,
  Empty,
  ErrorState,
  Heading,
  Label,
  Loading,
  Panel,
  Property,
  Row,
  Screen,
  usePullRefresh,
} from "@/ui/kit";
import { age, dateTime, overdue, words } from "@/ui/format";
import { Markdown } from "@/ui/markdown";

type TaskEvent = Schemas["TaskEvent"];

function EventRow({ event }: { event: TaskEvent }) {
  return (
    <View style={{ gap: 2, paddingVertical: 6 }}>
      <Row gap={6}>
        <Label bold size={14}>
          {event.agent || "System / human"}
        </Label>
        <Label tone="accent" size={13}>
          {words(event.event_type)}
        </Label>
        <Label tone="muted" size={12}>
          {age(event.created_at)}
        </Label>
      </Row>
      {event.message ? <Markdown text={event.message} size={14} /> : null}
    </View>
  );
}

function Timeline({ id }: { id: number }) {
  const { api } = useSession();
  const events = useInfiniteQuery({
    queryKey: ["task", id, "events"],
    enabled: !!api,
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      unwrap(
        api!.GET("/api/v1/tasks/{id}/events", {
          params: { path: { id: String(id) }, query: pageParam ? { before: pageParam } : {} },
        }),
      ),
    getNextPageParam: (page) => page.next_before ?? undefined,
  });
  if (events.isLoading) return <Loading label="Loading history" />;
  if (events.error) return <ErrorState error={events.error} retry={() => void events.refetch()} />;
  const items = events.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <Panel>
      {items.length ? (
        items.map((event) => <EventRow key={event.id} event={event} />)
      ) : (
        <Empty title="No activity yet" />
      )}
      {events.hasNextPage ? (
        <Button
          small
          label="Load earlier activity"
          disabled={events.isFetchingNextPage}
          onPress={() => void events.fetchNextPage()}
        />
      ) : null}
    </Panel>
  );
}

export default function TaskScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { api } = useSession();
  const [tab, setTab] = useState<"overview" | "activity">("overview");
  const numeric = Number(id);
  const task = useQuery({
    queryKey: ["task", numeric],
    enabled: !!api && Number.isSafeInteger(numeric) && numeric > 0,
    queryFn: () =>
      unwrap(api!.GET("/api/v1/tasks/{id}", { params: { path: { id: String(numeric) } } })),
  });
  const data = task.data;
  const pull = usePullRefresh(() => task.refetch());
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: `Task #${id}` }} />
      {!data ? (
        task.error ? (
          <ErrorState error={task.error} retry={() => void task.refetch()} />
        ) : (
          <Loading label="Opening task…" />
        )
      ) : (
        <>
          <Row>
            <Badge status={data.status} />
            <Label tone="muted" size={13}>
              Priority {data.priority}
            </Label>
            {data.status === "pending" && data.is_ready ? (
              <Label tone="accent" size={13}>
                Ready
              </Label>
            ) : null}
          </Row>
          <Heading>{data.title}</Heading>
          <Choice
            value={tab}
            options={[
              { value: "overview", label: "Overview" },
              { value: "activity", label: "Activity" },
            ]}
            onChange={setTab}
          />
          {tab === "activity" ? (
            <Timeline id={data.id} />
          ) : (
            <>
              <Panel>
                <Property label="Assigned agent">
                  {data.claimed_by ? (
                    <Row gap={6}>
                      <Avatar name={data.claimed_by} />
                      <Label>{data.claimed_by}</Label>
                    </Row>
                  ) : (
                    "Unassigned"
                  )}
                </Property>
                <Property label="Last heartbeat">
                  <Label
                    tone={
                      data.status === "in_progress" && overdue(data.heartbeat_at)
                        ? "danger"
                        : "text"
                    }
                  >
                    {data.heartbeat_at
                      ? `${age(data.heartbeat_at)}${data.status === "in_progress" && overdue(data.heartbeat_at) ? " · overdue" : ""}`
                      : "—"}
                  </Label>
                </Property>
                <Property label="Project">{data.project}</Property>
                <Property label="Created by">{data.created_by || "Human / system"}</Property>
              </Panel>
              <Panel>
                <Label tone="muted" size={12} bold>
                  DESCRIPTION & ACCEPTANCE CRITERIA
                </Label>
                {data.description ? (
                  <Markdown text={data.description} />
                ) : (
                  <Label tone="muted">No description provided.</Label>
                )}
              </Panel>
              {data.blocked_reason ? (
                <Panel>
                  <Label tone="danger" size={12} bold>
                    BLOCKED REASON
                  </Label>
                  <Markdown text={data.blocked_reason} />
                </Panel>
              ) : null}
              {data.result_summary ? (
                <Panel>
                  <Label tone="accent" size={12} bold>
                    RESULT SUMMARY
                  </Label>
                  <Markdown text={data.result_summary} />
                </Panel>
              ) : null}
              <Panel>
                <Label tone="muted" size={12} bold>
                  PREREQUISITES · {data.dependency_progress.done}/{data.dependency_progress.total}
                </Label>
                {data.dependencies.length ? (
                  data.dependencies.map((dependency) => (
                    <Panel
                      key={dependency.id}
                      accessibilityLabel={`Open prerequisite ${dependency.id}: ${dependency.title}`}
                      onPress={() =>
                        router.push({ pathname: "/tasks/[id]", params: { id: dependency.id } })
                      }
                    >
                      <Row>
                        <Badge status={dependency.status} />
                        <Label tone="muted" size={13}>
                          #{dependency.id}
                        </Label>
                      </Row>
                      <Label>{dependency.title}</Label>
                    </Panel>
                  ))
                ) : (
                  <Label tone="muted">This task has no prerequisites.</Label>
                )}
              </Panel>
              <Panel>
                <Label tone="muted" size={12} bold>
                  TIMESTAMPS
                </Label>
                <Property label="Created">{dateTime(data.created_at)}</Property>
                <Property label="Claimed">{dateTime(data.claimed_at)}</Property>
                <Property label="Updated">{dateTime(data.updated_at)}</Property>
              </Panel>
              <Label tone="muted" size={12} style={{ textAlign: "center" }}>
                Changes are reported by agents in real time.
              </Label>
            </>
          )}
        </>
      )}
    </Screen>
  );
}
