import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { router, Stack } from "expo-router";
import { ApiError, unwrap } from "@/api/client";
import type { Task } from "@/api/client";
import { useSession } from "@/auth/session";
import { useLive } from "@/realtime/useLive";
import { STATUS_LABELS, usePalette } from "@/ui/theme";

const PAGE = 20;

export default function Tasks() {
  const { api, user, signOut, expire } = useSession();
  const palette = usePalette();
  const [items, setItems] = useState<Task[]>([]);
  const [count, setCount] = useState<number | null>(null);
  // The first page starts loading as soon as the screen mounts.
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(
    async (offset: number) => {
      if (!api) return;
      try {
        const page = await unwrap(
          api.GET("/api/v1/tasks", {
            params: { query: { limit: PAGE, offset, ordering: "priority" } },
          }),
        );
        setError("");
        setCount(page.count);
        setItems((current) => (offset === 0 ? page.items : [...current, ...page.items]));
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          // Token revoked or account deactivated: back to sign-in.
          await expire();
          router.replace("/login");
          return;
        }
        setError(e instanceof Error ? e.message : "Could not load tasks");
      }
    },
    [api, expire],
  );

  useEffect(() => {
    if (!api) {
      router.replace("/login");
      return;
    }
    let active = true;
    const first = async () => {
      await load(0);
      if (active) setLoading(false);
    };
    void first();
    return () => {
      active = false;
    };
  }, [api, load]);

  // Live while open: task changes reload the first page (#55).
  const reloadSoon = useRef<ReturnType<typeof setTimeout> | null>(null);
  const live = useLive(
    useCallback(
      (event) => {
        if (event.subject !== "tasks" && !event.task_id) return;
        if (reloadSoon.current) clearTimeout(reloadSoon.current);
        reloadSoon.current = setTimeout(() => void load(0), 400);
      },
      [load],
    ),
  );

  const refresh = async () => {
    setRefreshing(true);
    await load(0);
    setRefreshing(false);
  };
  const more = async () => {
    if (loading || count === null || items.length >= count) return;
    setLoading(true);
    await load(items.length);
    setLoading(false);
  };
  const leave = async () => {
    await signOut();
    router.replace("/login");
  };

  return (
    <View style={{ flex: 1, backgroundColor: palette.bg }}>
      <Stack.Screen
        options={{
          title: "Tasks",
          headerLeft: () => (
            <Pressable
              accessibilityRole="button"
              onPress={() => router.push("/approvals")}
              hitSlop={10}
            >
              <Text style={{ color: palette.accent, fontSize: 16 }}>Approvals</Text>
            </Pressable>
          ),
          headerRight: () => (
            <Pressable accessibilityRole="button" onPress={leave} hitSlop={10}>
              <Text style={{ color: palette.accent, fontSize: 16 }}>Sign out</Text>
            </Pressable>
          ),
        }}
      />
      <FlatList
        data={items}
        keyExtractor={(task) => String(task.id)}
        contentContainerStyle={{ padding: 16, gap: 10 }}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={palette.accent} />
        }
        onEndReached={more}
        onEndReachedThreshold={0.4}
        ListHeaderComponent={
          <View style={{ marginBottom: 6 }}>
            <Text style={{ color: palette.secondary }}>
              {live === "live" ? "● Live · " : live === "connecting" ? "Connecting… · " : ""}
              {user ? `${user.username} · ${user.role}` : ""}
              {count !== null ? ` · ${count} tasks` : ""}
            </Text>
            {error ? (
              <Text accessibilityRole="alert" style={{ color: palette.danger, marginTop: 8 }}>
                {error}
              </Text>
            ) : null}
          </View>
        }
        ListEmptyComponent={
          loading ? undefined : (
            <Text style={{ color: palette.muted, textAlign: "center", marginTop: 40 }}>
              No tasks yet.
            </Text>
          )
        }
        ListFooterComponent={loading ? <ActivityIndicator color={palette.accent} /> : undefined}
        renderItem={({ item }) => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Open task: ${item.title}`}
            onPress={() => router.push({ pathname: "/tasks/[id]", params: { id: item.id } })}
            style={({ pressed }) => [
              styles.card,
              {
                backgroundColor: palette.panel,
                borderColor: palette.line,
                opacity: pressed ? 0.8 : 1,
              },
            ]}
          >
            <View style={styles.row}>
              <Text style={[styles.meta, { color: palette.muted }]}>
                #{item.id} · {item.project}
              </Text>
              <Text style={[styles.meta, { color: palette.accent }]}>
                {STATUS_LABELS[item.status] ?? item.status}
              </Text>
            </View>
            <Text style={[styles.title, { color: palette.text }]} numberOfLines={2}>
              {item.title}
            </Text>
            <Text style={[styles.meta, { color: palette.secondary }]}>
              Priority {item.priority}
              {item.claimed_by ? ` · ${item.claimed_by}` : " · unclaimed"}
            </Text>
          </Pressable>
        )}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderWidth: 1, borderRadius: 10, padding: 14, gap: 6 },
  row: { flexDirection: "row", justifyContent: "space-between" },
  title: { fontSize: 16, fontWeight: "600" },
  meta: { fontSize: 13 },
});
