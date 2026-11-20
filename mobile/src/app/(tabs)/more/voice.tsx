// The PM router (owners only), like the web's Voice agent: talk or type about
// projects, create and launch tasks, and check progress. Task changes are made
// by the server's Task Manager tools; this screen only refreshes task views.
import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  AppState,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  Text,
  TextInput,
  View,
} from "react-native";
import { Stack } from "expo-router";
import type { ScrollViewInstance } from "react-native";
import { useAudioStream } from "expo-audio";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { unwrap } from "@/api/client";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import { Button, Label, Notice, Panel, Row, styles } from "@/ui/kit";
import { usePalette } from "@/ui/theme";
import { WAV_RATE } from "@/voice/audio";
import { PMConversation } from "@/voice/conversation";
import type { PMStatus } from "@/voice/conversation";

const SUGGESTIONS = [
  "What projects need my attention?",
  "What tasks are pending in agents-task-manager?",
];

/** Refresh every task representation together, like the web's refreshTaskViews. */
function refreshTaskViews(client: QueryClient) {
  return Promise.all(
    ["planning", "workspace", "tasks", "task"].map((key) =>
      client.invalidateQueries({ queryKey: [key] }),
    ),
  );
}

function Service({ label, ready, state }: { label: string; ready: boolean; state: string }) {
  const palette = usePalette();
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: 6,
        paddingHorizontal: 10,
        paddingVertical: 4,
        borderRadius: 999,
        borderWidth: 1,
        borderColor: palette.line,
        backgroundColor: palette.raised,
      }}
    >
      <View
        style={{
          width: 7,
          height: 7,
          borderRadius: 4,
          backgroundColor: ready ? palette.accent : palette.muted,
        }}
      />
      <Label size={12} tone="secondary">
        {label} · {state}
      </Label>
    </View>
  );
}

export default function VoiceAgent() {
  const { api } = useSession();
  const { canManage } = useAccess();
  const palette = usePalette();
  const insets = useSafeAreaInsets();
  const headerHeight = Platform.OS === "ios" ? insets.top + 44 : 0;
  const queries = useQueryClient();
  const [, render] = useReducer((value: number) => value + 1, 0);
  const [draft, setDraft] = useState("");
  const [holdSize, setHoldSize] = useState({ width: 0, height: 0 });
  const transcript = useRef<ScrollViewInstance>(null);
  const { stream } = useAudioStream({ sampleRate: WAV_RATE, channels: 1, encoding: "int16" });

  // Constructing the client has no side effects; the effect owns its lifetime.
  const client = useMemo(
    () =>
      api
        ? new PMConversation(api, stream, render, () => {
            void refreshTaskViews(queries);
          })
        : undefined,
    [api, stream, queries],
  );
  useEffect(() => {
    client?.open();
    return () => client?.close();
  }, [client]);

  // Leaving the app discards a clip in progress, like the web on blur.
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      client?.appStateChanged(state);
    });
    return () => subscription.remove();
  }, [client]);

  const status = useQuery({
    queryKey: ["voice-status"],
    enabled: !!api && canManage,
    queryFn: async ({ signal }) =>
      (await unwrap(api!.GET("/api/v1/voice/status", { signal }))) as PMStatus,
    refetchInterval: 30_000,
    retry: false,
  });

  if (!canManage)
    return (
      <View style={{ flex: 1, backgroundColor: palette.bg, padding: 16 }}>
        <Stack.Screen options={{ title: "Voice agent" }} />
        <Notice>Only owners can use the PM voice agent.</Notice>
      </View>
    );

  const connected = client?.status === "connected";
  const busy = client?.status === "connecting" || Boolean(client?.pending);
  const ready = Boolean(status.data?.configured);
  const speechReady = Boolean(status.data?.speech_configured);
  const recording = Boolean(client?.capturing);
  const holdDisabled = !ready || !speechReady || (busy && !recording);
  const holdLabel = recording
    ? client?.captureReady
      ? "Recording — release to send"
      : "Starting microphone…"
    : busy
      ? "Processing…"
      : client?.error
        ? "Hold to retry"
        : "Hold to talk";
  const send = () => {
    if (!draft.trim()) return;
    void client?.send(draft);
    setDraft("");
  };
  const errorText =
    client?.error ||
    (status.error instanceof Error ? status.error.message : "") ||
    (!status.isPending && !ready
      ? "Fireworks PM reasoning is not configured on the backend yet."
      : "");

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: palette.bg }}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={headerHeight}
    >
      <Stack.Screen options={{ title: "Voice agent" }} />
      <ScrollView
        ref={transcript}
        contentContainerStyle={{ padding: 16, gap: 10 }}
        keyboardShouldPersistTaps="handled"
        onContentSizeChange={() => transcript.current?.scrollToEnd({ animated: true })}
        accessibilityLabel="Conversation transcript"
      >
        <View style={{ gap: 4 }}>
          <Label size={20} bold>
            PM router
          </Label>
          <Label tone="secondary" size={13}>
            Talk naturally about projects, create tasks, and check progress.
          </Label>
        </View>
        <Row gap={6}>
          <Service
            label="Fireworks reasoning"
            ready={ready}
            state={status.isPending ? "checking" : ready ? "configured" : "unavailable"}
          />
          <Service
            label="ElevenLabs transcription"
            ready={Boolean(status.data?.transcription_configured)}
            state={status.data?.transcription_configured ? "configured" : "unavailable"}
          />
          <Service
            label="ElevenLabs TTS"
            ready={Boolean(status.data?.tts_configured)}
            state={status.data?.tts_configured ? "configured" : "unavailable"}
          />
          <Service
            label="PM agent"
            ready={connected}
            state={connected ? "connected" : busy ? "connecting" : "not connected"}
          />
        </Row>
        <Row>
          <Button small label="Check connection" onPress={() => void status.refetch()} />
          <Button
            small
            label="End conversation"
            disabled={!connected && !busy}
            onPress={() => void client?.stop()}
          />
          <Button
            small
            label="Clear conversation"
            disabled={!client?.entries.length && !connected}
            onPress={() => void client?.clear()}
          />
        </Row>
        {errorText ? <Notice error>{errorText}</Notice> : null}
        {!client?.entries.length ? (
          <Panel style={{ alignItems: "center", gap: 10, paddingVertical: 24 }}>
            <Text style={{ fontSize: 34 }}>🎙</Text>
            <Label size={17} bold>
              What would you like to work on?
            </Label>
            <Label tone="secondary" size={13} style={{ textAlign: "center" }}>
              Hold to talk, or type to chat without microphone access.
            </Label>
            {SUGGESTIONS.map((text) => (
              <Button
                key={text}
                small
                label={text}
                disabled={!ready || busy}
                onPress={() => void client?.send(text)}
              />
            ))}
          </Panel>
        ) : (
          client.entries.map((entry, index) => (
            <View
              key={index}
              style={{
                alignSelf: entry.role === "user" ? "flex-end" : "stretch",
                maxWidth: entry.role === "user" ? "85%" : "100%",
                padding: entry.role === "tool" ? 6 : 12,
                borderRadius: 12,
                borderWidth: entry.role === "tool" ? 0 : 1,
                borderColor: palette.line,
                backgroundColor:
                  entry.role === "user"
                    ? palette.raised
                    : entry.role === "tool"
                      ? "transparent"
                      : palette.panel,
                gap: 4,
              }}
            >
              <Label tone="muted" size={11} bold>
                {entry.role === "user"
                  ? "YOU"
                  : entry.role === "tool"
                    ? "TASK MANAGER TOOL"
                    : "PM AGENT"}
              </Label>
              <Label
                selectable
                size={entry.role === "tool" ? 12 : 15}
                tone={entry.role === "tool" ? "secondary" : "text"}
              >
                {entry.text || "…"}
              </Label>
            </View>
          ))
        )}
      </ScrollView>
      <View
        style={{
          borderTopWidth: 1,
          borderColor: palette.line,
          backgroundColor: palette.panel,
          padding: 12,
          paddingBottom: 12,
          gap: 8,
        }}
      >
        <Row wrap={false}>
          <View
            accessible
            accessibilityRole="button"
            accessibilityLabel="Push to talk"
            accessibilityHint="Hold while you speak and release to send. Slide off the button to discard."
            accessibilityState={{ disabled: holdDisabled, busy: recording }}
            onLayout={(event) => setHoldSize(event.nativeEvent.layout)}
            onStartShouldSetResponder={() => !holdDisabled}
            onResponderTerminationRequest={() => false}
            onResponderGrant={() => void client?.beginPushToTalk()}
            onResponderRelease={(event) => {
              const { locationX: x, locationY: y } = event.nativeEvent;
              const inside = x >= 0 && y >= 0 && x <= holdSize.width && y <= holdSize.height;
              if (inside) client?.endPushToTalk();
              else client?.cancelPushToTalk();
            }}
            onResponderTerminate={() => client?.cancelPushToTalk()}
            style={{
              flex: 1,
              alignItems: "center",
              justifyContent: "center",
              paddingVertical: 16,
              borderRadius: 12,
              backgroundColor: recording ? palette.danger : palette.accent,
              opacity: holdDisabled ? 0.45 : 1,
            }}
          >
            <Text style={{ color: palette.onAccent, fontWeight: "700", fontSize: 16 }}>
              🎙 {holdLabel}
            </Text>
          </View>
          <Button
            label={client?.muted ? "🔇" : "🔊"}
            accessibilityLabel={client?.muted ? "Enable spoken replies" : "Mute spoken replies"}
            onPress={() => client?.setMuted(!client.muted)}
          />
        </Row>
        <Row wrap={false}>
          <TextInput
            accessibilityLabel="Message PM agent"
            placeholder="Ask about projects or describe a task…"
            placeholderTextColor={palette.muted}
            value={draft}
            onChangeText={setDraft}
            maxLength={4000}
            returnKeyType="send"
            onSubmitEditing={send}
            style={[
              styles.input,
              {
                flex: 1,
                color: palette.text,
                borderColor: palette.line,
                backgroundColor: palette.bg,
              },
            ]}
          />
          <Button
            kind="primary"
            label="Send"
            accessibilityLabel="Send message"
            disabled={!ready || busy || !draft.trim()}
            onPress={send}
          />
        </Row>
        <Text accessibilityLiveRegion="polite" style={{ color: palette.muted, fontSize: 12 }}>
          {client?.notice || client?.state || "Ready to talk"}
        </Text>
      </View>
    </KeyboardAvoidingView>
  );
}
