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
import { Stack } from "expo-router";
import { unwrap } from "@/api/client";
import type { Schemas } from "@/api/client";
import { useSession } from "@/auth/session";
import { useLive } from "@/realtime/useLive";
import type { ChangeEvent } from "@/realtime/useLive";
import { usePalette } from "@/ui/theme";
import type { Palette } from "@/ui/theme";

type Interaction = Schemas["PendingInteractionCard"];
type ReplyAction = "permission.reply" | "question.reply" | "question.reject";
interface Question {
  header: string;
  text: string;
  options: { label: string; description: string }[];
  multiple: boolean;
}

function isApprovalEvent(event: ChangeEvent) {
  const type = event.event_type ?? "";
  return (
    event.subject === "workers" ||
    event.subject === "sessions" ||
    /^(interaction|permission|question|command)\./.test(type)
  );
}

function newRequestId() {
  return `mobile-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function Button({
  label,
  onPress,
  disabled,
  palette,
  primary = false,
}: {
  label: string;
  onPress: () => void;
  disabled: boolean;
  palette: Palette;
  primary?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        {
          backgroundColor: primary ? palette.accent : palette.raised,
          borderColor: palette.line,
          opacity: disabled ? 0.45 : pressed ? 0.8 : 1,
        },
      ]}
    >
      <Text style={{ color: primary ? palette.onAccent : palette.text, fontWeight: "600" }}>
        {label}
      </Text>
    </Pressable>
  );
}

function Card({
  item,
  canAnswer,
  busy,
  answer,
  palette,
}: {
  item: Interaction;
  canAnswer: boolean;
  busy: boolean;
  answer: (item: Interaction, action: ReplyAction, payload: Record<string, unknown>) => void;
  palette: Palette;
}) {
  const questions = item.questions as Question[];
  const [choices, setChoices] = useState<string[][]>(() => questions.map(() => []));
  const locked = !canAnswer || busy || item.replyDispatched;
  const toggle = (index: number, label: string, multiple: boolean) =>
    setChoices((current) =>
      current.map((selected, i) =>
        i !== index
          ? selected
          : multiple
            ? selected.includes(label)
              ? selected.filter((value) => value !== label)
              : [...selected, label]
            : [label],
      ),
    );
  const complete = choices.every((selected) => selected.length > 0);
  return (
    <View style={[styles.card, { backgroundColor: palette.panel, borderColor: palette.line }]}>
      <Text style={{ color: palette.muted, fontSize: 13 }}>
        {item.worker}
        {item.sessionTitle ? ` · ${item.sessionTitle}` : ""}
      </Text>
      {item.kind === "permission" ? (
        <>
          <Text style={[styles.title, { color: palette.text }]}>
            {item.title || item.type || "Permission required"}
          </Text>
          {item.description ? (
            <Text style={{ color: palette.secondary }}>{item.description}</Text>
          ) : null}
          {item.patterns.length ? (
            <Text style={{ color: palette.secondary, fontFamily: "Menlo" }}>
              {item.patterns.join("\n")}
            </Text>
          ) : null}
          <View style={styles.row}>
            <Button
              label="Allow once"
              primary
              palette={palette}
              disabled={locked}
              onPress={() => answer(item, "permission.reply", { reply: "once" })}
            />
            <Button
              label="Always"
              palette={palette}
              disabled={locked}
              onPress={() => answer(item, "permission.reply", { reply: "always" })}
            />
            <Button
              label="Reject"
              palette={palette}
              disabled={locked}
              onPress={() => answer(item, "permission.reply", { reply: "reject" })}
            />
          </View>
        </>
      ) : (
        <>
          <Text style={[styles.title, { color: palette.text }]}>Answers needed</Text>
          {questions.map((question, index) => (
            <View key={index} style={{ gap: 6 }}>
              <Text style={{ color: palette.text, fontWeight: "600" }}>
                {question.header || `Question ${index + 1}`}
              </Text>
              <Text style={{ color: palette.secondary }}>{question.text}</Text>
              <View style={styles.row}>
                {question.options.map((option) => {
                  const selected = choices[index]?.includes(option.label);
                  return (
                    <Pressable
                      key={option.label}
                      accessibilityRole={question.multiple ? "checkbox" : "radio"}
                      accessibilityState={{ checked: selected, disabled: locked }}
                      disabled={locked}
                      onPress={() => toggle(index, option.label, question.multiple)}
                      style={[
                        styles.chip,
                        {
                          borderColor: selected ? palette.accent : palette.line,
                          backgroundColor: selected ? palette.raised : "transparent",
                        },
                      ]}
                    >
                      <Text style={{ color: palette.text }}>{option.label}</Text>
                    </Pressable>
                  );
                })}
              </View>
            </View>
          ))}
          <View style={styles.row}>
            <Button
              label="Submit answers"
              primary
              palette={palette}
              disabled={locked || !complete}
              onPress={() => answer(item, "question.reply", { answers: choices })}
            />
            <Button
              label="Dismiss"
              palette={palette}
              disabled={locked}
              onPress={() => answer(item, "question.reject", {})}
            />
          </View>
        </>
      )}
      {item.replyDispatched ? (
        <Text style={{ color: palette.accent }}>Answer sent · waiting for the worker</Text>
      ) : null}
    </View>
  );
}

export default function Approvals() {
  const { api, user } = useSession();
  const palette = usePalette();
  const canAnswer = user?.role === "member" || user?.role === "owner";
  const [items, setItems] = useState<Interaction[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    if (!api) return;
    try {
      const page = await unwrap(api.GET("/api/v1/interactions"));
      setItems(page.items);
    } catch (e) {
      setMessage({
        text: e instanceof Error ? e.message : "Could not load approvals",
        error: true,
      });
    }
  }, [api]);

  useEffect(() => {
    const first = async () => {
      await load();
    };
    void first();
  }, [load]);

  const live = useLive(
    useCallback(
      (event: ChangeEvent) => {
        if (!isApprovalEvent(event)) return;
        // Coalesce bursts of worker events into one refresh.
        if (pending.current) clearTimeout(pending.current);
        pending.current = setTimeout(() => void load(), 300);
      },
      [load],
    ),
  );

  async function answer(item: Interaction, action: ReplyAction, payload: Record<string, unknown>) {
    if (!api) return;
    setBusy(item.id);
    setMessage(null);
    try {
      await unwrap(
        api.POST("/api/v1/commands", {
          body: {
            worker: item.worker,
            action,
            sessionID: item.sessionID ?? "",
            payload: { requestID: item.id, ...payload },
            client_request_id: newRequestId(),
          },
        }),
      );
      setMessage({ text: "Answer queued for the worker.", error: false });
      await load();
    } catch (e) {
      setMessage({
        text: e instanceof Error ? e.message : "Could not send the answer",
        error: true,
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <View style={{ flex: 1, backgroundColor: palette.bg }}>
      <Stack.Screen options={{ title: "Approvals" }} />
      <FlatList
        data={items ?? []}
        keyExtractor={(item) => `${item.worker}:${item.kind}:${item.id}`}
        contentContainerStyle={{ padding: 16, gap: 12 }}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            tintColor={palette.accent}
            onRefresh={async () => {
              setRefreshing(true);
              await load();
              setRefreshing(false);
            }}
          />
        }
        ListHeaderComponent={
          <View style={{ gap: 6 }}>
            <Text style={{ color: palette.secondary }}>
              {live === "live" ? "● Live" : live === "connecting" ? "Connecting…" : "Offline"}
              {items ? ` · ${items.length} waiting` : ""}
            </Text>
            {!canAnswer ? (
              <Text accessibilityRole="text" style={{ color: palette.muted }}>
                Viewers can see requests; members and owners answer them.
              </Text>
            ) : null}
            {message ? (
              <Text
                accessibilityRole={message.error ? "alert" : "text"}
                style={{ color: message.error ? palette.danger : palette.accent }}
              >
                {message.text}
              </Text>
            ) : null}
          </View>
        }
        ListEmptyComponent={
          items === null ? (
            <ActivityIndicator color={palette.accent} />
          ) : (
            <Text style={{ color: palette.muted, textAlign: "center", marginTop: 40 }}>
              Nothing needs your attention.
            </Text>
          )
        }
        renderItem={({ item }) => (
          <Card
            item={item}
            canAnswer={canAnswer}
            busy={busy === item.id}
            answer={answer}
            palette={palette}
          />
        )}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderWidth: 1, borderRadius: 10, padding: 14, gap: 10 },
  title: { fontSize: 16, fontWeight: "600" },
  row: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  button: { borderWidth: 1, borderRadius: 8, paddingHorizontal: 14, paddingVertical: 10 },
  chip: { borderWidth: 1, borderRadius: 16, paddingHorizontal: 12, paddingVertical: 6 },
});
