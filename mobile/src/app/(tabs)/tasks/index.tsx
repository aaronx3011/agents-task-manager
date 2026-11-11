import { FlatList, View } from "react-native";
import { router, Stack } from "expo-router";
import { useInfiniteQuery } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import { Badge, Empty, ErrorState, Label, LiveStatus, Loading, Panel, Row } from "@/ui/kit";
import { usePalette } from "@/ui/theme";

const PAGE = 20;

export default function Tasks() {
  const { api } = useSession();
  const { username, role } = useAccess();
  const palette = usePalette();
  const tasks = useInfiniteQuery({
    queryKey: ["tasks", "list"],
    enabled: !!api,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api!.GET("/api/v1/tasks", {
          params: { query: { limit: PAGE, offset: pageParam, ordering: "priority" } },
        }),
      ),
    getNextPageParam: (page, pages) => {
      const loaded = pages.reduce((sum, p) => sum + p.items.length, 0);
      return loaded < page.count ? loaded : undefined;
    },
  });
  const items = tasks.data?.pages.flatMap((page) => page.items) ?? [];
  const count = tasks.data?.pages[0]?.count;
  return (
    <View style={{ flex: 1, backgroundColor: palette.bg }}>
      <Stack.Screen options={{ title: "Tasks" }} />
      <FlatList
        data={items}
        keyExtractor={(task) => String(task.id)}
        contentContainerStyle={{ padding: 16, gap: 10 }}
        refreshing={tasks.isRefetching && !tasks.isFetchingNextPage}
        onRefresh={() => void tasks.refetch()}
        onEndReached={() => tasks.hasNextPage && void tasks.fetchNextPage()}
        onEndReachedThreshold={0.4}
        ListHeaderComponent={
          <LiveStatus
            suffix={[username && `${username} · ${role}`, count !== undefined && `${count} tasks`]
              .filter(Boolean)
              .join(" · ")}
          />
        }
        ListEmptyComponent={
          tasks.isLoading ? (
            <Loading label="Loading tasks" />
          ) : tasks.error ? (
            <ErrorState error={tasks.error} retry={() => void tasks.refetch()} />
          ) : (
            <Empty title="No tasks yet" />
          )
        }
        ListFooterComponent={
          tasks.isFetchingNextPage ? <Loading label="Loading more" /> : undefined
        }
        renderItem={({ item }) => (
          <Panel
            accessibilityLabel={`Open task: ${item.title}`}
            onPress={() => router.push({ pathname: "/tasks/[id]", params: { id: item.id } })}
          >
            <Row style={{ justifyContent: "space-between" }}>
              <Label tone="muted" size={13}>
                #{item.id} · {item.project}
              </Label>
              <Badge status={item.status} />
            </Row>
            <Label size={16} bold numberOfLines={2}>
              {item.title}
            </Label>
            <Label tone="secondary" size={13}>
              Priority {item.priority}
              {item.claimed_by ? ` · ${item.claimed_by}` : " · unclaimed"}
            </Label>
          </Panel>
        )}
      />
    </View>
  );
}
