// The task workspace (#77): board by status or a sortable list, with search,
// filters and the agent strip, matching the web's All tasks view.
import { useEffect, useState } from "react";
import { FlatList, TextInput, View } from "react-native";
import { router, Stack } from "expo-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import { useSession } from "@/auth/session";
import { activeFilters, EMPTY_FILTERS, LANES, ORDERINGS, taskQuery } from "@/tasks/filters";
import type { Filters, Ordering, Status } from "@/tasks/filters";
import { AgentStrip, FilterSheet, TaskCard } from "@/tasks/parts";
import {
  Button,
  Choice,
  Empty,
  ErrorState,
  Label,
  LiveStatus,
  Loading,
  Row,
  styles,
  usePullRefresh,
} from "@/ui/kit";
import { STATUS_LABELS, usePalette } from "@/ui/theme";

const PAGE = 20;

export default function Tasks() {
  const { api } = useSession();
  const palette = usePalette();
  const [view, setView] = useState<"board" | "list">("board");
  const [chosenLane, setLane] = useState<Status | null>(null);
  const [ordering, setOrdering] = useState<Ordering>("priority");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [search, setSearch] = useState("");
  const [filtering, setFiltering] = useState(false);
  const [showAgents, setShowAgents] = useState(true);

  // Search as you type, without a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(
      () => setFilters((current) => ({ ...current, q: search.trim() })),
      350,
    );
    return () => clearTimeout(timer);
  }, [search]);

  const workspace = useQuery({
    queryKey: ["workspace", filters],
    enabled: !!api,
    queryFn: () =>
      unwrap(api!.GET("/api/v1/dashboard/workspace", { params: { query: taskQuery(filters) } })),
  });
  const data = workspace.data;
  // Until a lane is picked, open the first one that has tasks.
  const lane: Status =
    chosenLane ?? LANES.find((status) => (data?.counts[status] ?? 0) > 0) ?? "pending";

  // A status filter on the board shows just that lane (cancelled is list-only on the web).
  const boardStatus: Status = filters.status || lane;
  const extra =
    view === "board" ? { status: boardStatus, ordering: "priority" as Ordering } : { ordering };
  const tasks = useInfiniteQuery({
    queryKey: ["tasks", view, filters, extra],
    enabled: !!api,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api!.GET("/api/v1/tasks", {
          params: { query: taskQuery(filters, { ...extra, limit: PAGE, offset: pageParam }) },
        }),
      ),
    getNextPageParam: (page, pages) => {
      const loaded = pages.reduce((sum, p) => sum + p.items.length, 0);
      return loaded < page.count ? loaded : undefined;
    },
  });
  const items = tasks.data?.pages.flatMap((page) => page.items) ?? [];
  const count = tasks.data?.pages[0]?.count;
  const filterCount = activeFilters(filters);
  const open = (id: number) => router.push({ pathname: "/tasks/[id]", params: { id } });
  const refresh = () => {
    void workspace.refetch();
    void tasks.refetch();
  };
  const pull = usePullRefresh(() => Promise.all([workspace.refetch(), tasks.refetch()]));

  const header = (
    <View style={{ gap: 12, marginBottom: 4 }}>
      <TextInput
        accessibilityLabel="Search tasks"
        placeholder="Search tasks"
        placeholderTextColor={palette.muted}
        value={search}
        onChangeText={setSearch}
        returnKeyType="search"
        clearButtonMode="while-editing"
        style={[
          styles.input,
          { color: palette.text, borderColor: palette.line, backgroundColor: palette.panel },
        ]}
      />
      <Row style={{ justifyContent: "space-between" }}>
        <Choice
          value={view}
          options={[
            { value: "board", label: "Board" },
            { value: "list", label: "List" },
          ]}
          onChange={setView}
        />
        <Button
          small
          label={filterCount ? `Filters (${filterCount})` : "Filters"}
          onPress={() => setFiltering(true)}
        />
      </Row>
      <LiveStatus
        suffix={
          data
            ? `${data.total} matching · ${data.ready_count} ready${view === "list" ? ` · ${ORDERINGS.find((o) => o.value === ordering)?.label}` : ""}`
            : undefined
        }
      />
      {/* One "cannot reach the server" is enough when both requests fail. */}
      {workspace.error && !tasks.error ? (
        <ErrorState error={workspace.error} retry={refresh} />
      ) : null}
      {data?.agents.length ? (
        <View style={{ gap: 6 }}>
          <Row style={{ justifyContent: "space-between" }}>
            <Label tone="muted" size={12} bold>
              THE TEAM · {data.agents.length}
            </Label>
            <Button
              small
              kind="link"
              label={showAgents ? "Hide" : "Show"}
              onPress={() => setShowAgents(!showAgents)}
            />
          </Row>
          {showAgents ? (
            <AgentStrip
              agents={data.agents}
              selected={filters.claimed_by}
              onFilter={(claimed_by) => setFilters((current) => ({ ...current, claimed_by }))}
              onOpen={open}
            />
          ) : null}
        </View>
      ) : null}
      {view === "board" ? (
        filters.status ? (
          <Label tone="secondary" size={13}>
            Showing {STATUS_LABELS[filters.status]} only (status filter)
          </Label>
        ) : (
          <Choice
            value={lane}
            options={LANES.map((status) => ({
              value: status,
              label: `${STATUS_LABELS[status]} ${data ? data.counts[status] : ""}`.trim(),
            }))}
            onChange={setLane}
          />
        )
      ) : null}
      {view === "board" && !filters.status && data?.counts.cancelled ? (
        <Button
          small
          kind="link"
          label={`${data.counts.cancelled} cancelled · view list`}
          onPress={() => {
            setView("list");
            setFilters((current) => ({ ...current, status: "cancelled" }));
          }}
        />
      ) : null}
    </View>
  );

  return (
    <View style={{ flex: 1, backgroundColor: palette.bg }}>
      <Stack.Screen options={{ title: "Tasks" }} />
      <FlatList
        data={items}
        keyExtractor={(task) => String(task.id)}
        contentContainerStyle={{ padding: 16, gap: 10 }}
        keyboardShouldPersistTaps="handled"
        refreshing={pull.refreshing}
        onRefresh={pull.onRefresh}
        onEndReached={() => tasks.hasNextPage && void tasks.fetchNextPage()}
        onEndReachedThreshold={0.4}
        ListHeaderComponent={header}
        ListEmptyComponent={
          tasks.isLoading ? (
            <Loading label="Loading tasks" />
          ) : tasks.error ? (
            <ErrorState error={tasks.error} retry={refresh} />
          ) : (
            <Empty
              title={filterCount || filters.q ? "No matching tasks" : "Nothing here yet"}
              detail={filterCount || filters.q ? "Try clearing filters or search." : undefined}
            />
          )
        }
        ListFooterComponent={
          tasks.isFetchingNextPage ? (
            <Loading label="Loading more" />
          ) : count !== undefined && items.length ? (
            <Label tone="muted" size={12} style={{ textAlign: "center" }}>
              {items.length} of {count}
            </Label>
          ) : undefined
        }
        renderItem={({ item }) => <TaskCard task={item} onPress={() => open(item.id)} />}
      />
      {filtering ? (
        <FilterSheet
          visible
          close={() => setFiltering(false)}
          filters={filters}
          ordering={ordering}
          showOrdering={view === "list"}
          projects={data?.projects ?? []}
          agents={data?.agents ?? []}
          apply={(next, order) => {
            setFilters({ ...next, q: filters.q });
            setOrdering(order);
          }}
        />
      ) : null}
    </View>
  );
}
