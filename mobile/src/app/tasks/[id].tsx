import { useEffect, useState } from "react";
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useLocalSearchParams } from "expo-router";
import { unwrap } from "@/api/client";
import type { TaskDetail } from "@/api/client";
import { useSession } from "@/auth/session";
import { STATUS_LABELS, usePalette } from "@/ui/theme";

export default function TaskScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { api } = useSession();
  const palette = usePalette();
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!api || !id) return;
    unwrap(api.GET("/api/v1/tasks/{id}", { params: { path: { id: String(id) } } }))
      .then(setTask)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load task"));
  }, [api, id]);

  if (error) return <Text style={{ color: palette.danger, padding: 20 }}>{error}</Text>;
  if (!task)
    return (
      <View style={{ flex: 1, justifyContent: "center" }}>
        <ActivityIndicator color={palette.accent} />
      </View>
    );
  const fact = (label: string, value: string) => (
    <View style={styles.fact}>
      <Text style={{ color: palette.muted, fontSize: 12 }}>{label}</Text>
      <Text style={{ color: palette.text }}>{value}</Text>
    </View>
  );
  return (
    <ScrollView contentContainerStyle={{ padding: 16, gap: 16 }}>
      <Stack.Screen options={{ title: `Task #${task.id}` }} />
      <Text style={[styles.title, { color: palette.text }]}>{task.title}</Text>
      <View style={[styles.panel, { backgroundColor: palette.panel, borderColor: palette.line }]}>
        {fact("Status", STATUS_LABELS[task.status] ?? task.status)}
        {fact("Project", task.project)}
        {fact("Priority", String(task.priority))}
        {fact("Claimed by", task.claimed_by ?? "Unclaimed")}
        {task.blocked_reason ? fact("Blocked because", task.blocked_reason) : null}
        {task.result_summary ? fact("Result", task.result_summary) : null}
      </View>
      {task.description ? (
        <View style={[styles.panel, { backgroundColor: palette.panel, borderColor: palette.line }]}>
          <Text style={{ color: palette.muted, fontSize: 12 }}>Description</Text>
          <Text style={{ color: palette.text, lineHeight: 21 }}>{task.description}</Text>
        </View>
      ) : null}
      <View style={[styles.panel, { backgroundColor: palette.panel, borderColor: palette.line }]}>
        <Text style={{ color: palette.muted, fontSize: 12 }}>Recent activity</Text>
        {task.recent_events.length === 0 ? (
          <Text style={{ color: palette.secondary }}>No events yet.</Text>
        ) : (
          task.recent_events.map((event) => (
            <View key={event.id} style={{ gap: 2 }}>
              <Text style={{ color: palette.text }}>
                {event.event_type}
                {event.agent ? ` · ${event.agent}` : ""}
              </Text>
              {event.message ? (
                <Text style={{ color: palette.secondary }} numberOfLines={4}>
                  {event.message}
                </Text>
              ) : null}
            </View>
          ))
        )}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  title: { fontSize: 22, fontWeight: "700" },
  panel: { borderWidth: 1, borderRadius: 10, padding: 14, gap: 10 },
  fact: { gap: 2 },
});
