// One OpenCode conversation (#78): live messages, older history, pending
// permission and question requests, prompt/interrupt, like the web SessionPanel.
import { useEffect, useRef, useState } from "react";
import { FlatList, KeyboardAvoidingView, Platform, TextInput, View } from "react-native";
import { router, Stack, useLocalSearchParams } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { mergeLiveMessages } from "@/api/types";
import type { Message } from "@/api/types";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import { useFocusedSession } from "@/live/live";
import {
  fetchMessages,
  modelPayload,
  usable,
  useCommandQueue,
  useLatestMessages,
  useSessionDetail,
  useWorker,
} from "@/sessions/api";
import { useSessionLibrary } from "@/sessions/library";
import { openSession } from "@/sessions/nav";
import { CommandFeedback, InteractionForm, MessageView, ModelPicker } from "@/sessions/parts";
import { Button, Empty, ErrorState, Label, Loading, Notice, Row, Toggle, styles } from "@/ui/kit";
import { errorText } from "@/ui/format";
import { usePalette } from "@/ui/theme";

export default function Chat() {
  const { worker: workerName = "", sid = "" } = useLocalSearchParams<{
    worker: string;
    sid: string;
  }>();
  const { api } = useSession();
  const { canWrite } = useAccess();
  const palette = usePalette();
  const library = useSessionLibrary();
  const queue = useCommandQueue();
  const [text, setText] = useState("");
  const [model, setModel] = useState("");
  const [noReply, setNoReply] = useState(false);
  const [options, setOptions] = useState(false);
  const [history, setHistory] = useState<{
    messages: Message[];
    cursor: string | null;
    gap: boolean;
  } | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<unknown>(null);
  const [following, setFollowing] = useState(true);
  const list = useRef<FlatList<Message>>(null);
  const live = useFocusedSession(workerName, sid);
  const worker = useWorker(workerName);
  const detail = useSessionDetail(workerName, sid);
  const messages = useLatestMessages(workerName, sid, !!live);
  // The stack header sits above this view on iOS: status bar plus a 44pt bar.
  const insets = useSafeAreaInsets();
  const headerHeight = Platform.OS === "ios" ? insets.top + 44 : 0;

  const title = detail.data?.title || sid;
  const observed = !!detail.data;
  const { remember } = library;
  useEffect(() => {
    if (observed) remember({ worker: workerName, sessionID: sid, title });
  }, [observed, remember, workerName, sid, title]);

  const displayed = mergeLiveMessages(
    [...(history?.messages ?? []), ...(messages.data?.messages ?? [])],
    live,
  );
  const cursor = history ? history.cursor : messages.data?.olderCursor;

  async function loadOlder() {
    if (!cursor || loadingOlder || !api) return;
    setFollowing(false);
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const page = await fetchMessages(api, workerName, sid, cursor);
      setHistory((previous) => ({
        messages: mergeLiveMessages(
          [...page.messages, ...(previous?.messages ?? []), ...(messages.data?.messages ?? [])],
          null,
        ),
        cursor: page.olderCursor || null,
        gap: Boolean(previous?.gap || page.gap),
      }));
    } catch (error) {
      setOlderError(error);
    } finally {
      setLoadingOlder(false);
    }
  }

  if (worker.isLoading || detail.isLoading)
    return (
      <>
        <Stack.Screen options={{ title: "Session" }} />
        <Loading label="Opening session" />
      </>
    );
  if (worker.error || detail.error || !worker.data || !detail.data)
    return (
      <ErrorState
        error={worker.error || detail.error}
        retry={() => {
          void worker.refetch();
          void detail.refetch();
        }}
      />
    );
  const data = detail.data;
  const workerData = worker.data;
  const disabled = !canWrite || !usable(workerData) || queue.isPending;
  const pinned = library.isPinned({ worker: workerName, sessionID: sid });

  const header = (
    <View style={{ gap: 10, marginBottom: 6 }}>
      <Label size={18} bold>
        {data.title || sid}
      </Label>
      <Label tone="secondary" size={13}>
        {live?.executionStatus || data.executionStatus || "Unknown state"} ·{" "}
        {data.agentName || "Default agent"}
        {data.model ? ` · ${data.model.providerID}/${data.model.modelID}` : ""}
      </Label>
      <Label tone="muted" size={12} mono>
        {workerName} · {sid}
      </Label>
      {data.directory ? (
        <Label tone="muted" size={12} numberOfLines={1}>
          {data.directory}
        </Label>
      ) : null}
      <Label tone="muted" size={12}>
        {live ? "Live conversation · batched storage" : "Saved history · waiting for live updates"}
      </Label>
      <Row>
        <Button
          small
          label={pinned ? "Unpin session" : "Pin session"}
          onPress={() => library.togglePin({ worker: workerName, sessionID: sid, title })}
        />
        <Button
          small
          kind="danger"
          label="Interrupt"
          disabled={disabled}
          onPress={() =>
            queue.mutate({ worker: workerName, action: "interrupt", sessionID: sid, payload: {} })
          }
        />
        {data.parentID ? (
          <Button
            small
            label="Parent session"
            onPress={() => openSession({ worker: workerName, sessionID: data.parentID! })}
          />
        ) : null}
        {data.subagents?.map((id) => (
          <Button
            key={id}
            small
            label={`Subagent ${id.slice(-6)}`}
            accessibilityLabel={`Subagent ${id}`}
            onPress={() => openSession({ worker: workerName, sessionID: id })}
          />
        ))}
        {data.taskLinks?.map((id) => (
          <Button
            key={id}
            small
            label={`Task #${id}`}
            onPress={() => router.push({ pathname: "/tasks/[id]", params: { id } })}
          />
        ))}
      </Row>
      {data.lastError ? <Notice error>{data.lastError}</Notice> : null}
      {!usable(workerData) ? (
        <Notice>This worker is not connected; controls are unavailable.</Notice>
      ) : null}
      {data.pendingInteractions?.map((request) => {
        const reply = data.commands?.find((command) => command.id === request.replyCommandId);
        const locked =
          disabled ||
          (request.replyDispatched &&
            (reply?.status !== "error" || Boolean(reply.deliveryUncertain)));
        return (
          <InteractionForm
            key={`${request.kind}/${request.id}`}
            request={request}
            worker={workerName}
            queue={queue}
            disabled={locked}
          />
        );
      })}
      {cursor ? (
        <Button
          small
          label={loadingOlder ? "Loading older messages…" : "Load older messages"}
          disabled={loadingOlder}
          onPress={() => void loadOlder()}
        />
      ) : null}
      {olderError ? (
        <Notice error>{errorText(olderError, "History could not be loaded")}</Notice>
      ) : null}
      <Label tone="muted" size={12}>
        Retained observation window · {displayed.length} messages ·{" "}
        {data.window.complete ? "Complete supplied window" : "History may be partial"}
        {messages.data?.gap || history?.gap ? " · Cursor gap: older data was pruned" : ""}
      </Label>
    </View>
  );

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: palette.bg }}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={headerHeight}
    >
      <Stack.Screen options={{ title: data.title || "Session" }} />
      <FlatList
        ref={list}
        data={displayed}
        keyExtractor={(message) => message.remoteID}
        contentContainerStyle={{ padding: 16, gap: 10 }}
        ListHeaderComponent={header}
        ListEmptyComponent={
          messages.isLoading ? (
            <Loading label="Loading messages" />
          ) : messages.error ? (
            <ErrorState error={messages.error} retry={() => void messages.refetch()} />
          ) : (
            <Empty title="No messages observed yet" />
          )
        }
        ListFooterComponent={
          !cursor && displayed.length ? (
            <Label tone="muted" size={12} style={{ textAlign: "center" }}>
              Oldest retained messages reached.
            </Label>
          ) : undefined
        }
        renderItem={({ item }) => <MessageView message={item} />}
        onContentSizeChange={() => {
          if (following) list.current?.scrollToEnd({ animated: false });
        }}
        onScroll={(event) => {
          const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
          const nearBottom = contentSize.height - contentOffset.y - layoutMeasurement.height < 80;
          if (nearBottom !== following) setFollowing(nearBottom);
        }}
        scrollEventThrottle={100}
        keyboardShouldPersistTaps="handled"
      />
      {!following ? (
        <View style={{ position: "absolute", right: 16, bottom: 150 }}>
          <Button
            small
            label="↓ Latest"
            accessibilityLabel="Latest messages"
            onPress={() => {
              setFollowing(true);
              list.current?.scrollToEnd({ animated: true });
            }}
          />
        </View>
      ) : null}
      <View
        style={{
          borderTopWidth: 1,
          borderColor: palette.line,
          backgroundColor: palette.panel,
          padding: 12,
          gap: 8,
        }}
      >
        <CommandFeedback queue={queue} />
        {options ? (
          <>
            <ModelPicker
              worker={workerData}
              value={model}
              onChange={setModel}
              disabled={!canWrite}
            />
            <Toggle
              label="Record only (no model reply)"
              value={noReply}
              onChange={setNoReply}
              disabled={!canWrite}
            />
          </>
        ) : null}
        <Row wrap={false}>
          <TextInput
            accessibilityLabel="Message"
            placeholder="Send instructions to this OpenCode session…"
            placeholderTextColor={palette.muted}
            value={text}
            onChangeText={setText}
            multiline
            maxLength={20000}
            editable={canWrite}
            style={[
              styles.input,
              {
                flex: 1,
                maxHeight: 120,
                color: palette.text,
                borderColor: palette.line,
                backgroundColor: palette.bg,
              },
            ]}
          />
          <View style={{ gap: 6 }}>
            <Button
              kind="primary"
              label="Send"
              accessibilityLabel="Send message"
              disabled={disabled || !text.trim()}
              onPress={() =>
                queue.mutate(
                  {
                    worker: workerName,
                    action: "prompt",
                    sessionID: sid,
                    payload: {
                      text: text.trim(),
                      noReply,
                      ...modelPayload(model, workerData.models),
                    },
                  },
                  {
                    onSuccess: () => {
                      setText("");
                      setFollowing(true);
                    },
                  },
                )
              }
            />
            <Button
              small
              kind="link"
              label={options ? "Hide options" : "Options"}
              onPress={() => setOptions(!options)}
            />
          </View>
        </Row>
      </View>
    </KeyboardAvoidingView>
  );
}
