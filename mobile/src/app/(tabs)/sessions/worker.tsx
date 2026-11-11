// One OpenCode worker (#78): health, its sessions, waiting requests and
// starting a new session, like the web WorkerPanel.
import { useEffect, useRef, useState } from "react";
import { Stack, useLocalSearchParams } from "expo-router";
import { useAccess } from "@/auth/access";
import {
  health,
  modelPayload,
  usable,
  useCommandQueue,
  useCommandStatus,
  useWorker,
  useWorkerSessions,
} from "@/sessions/api";
import { useSessionLibrary } from "@/sessions/library";
import { openSession } from "@/sessions/nav";
import { CommandFeedback, ModelPicker, record } from "@/sessions/parts";
import {
  Button,
  Empty,
  ErrorState,
  Field,
  Heading,
  Label,
  Loading,
  Notice,
  Panel,
  Row,
  Screen,
  Sheet,
  usePullRefresh,
} from "@/ui/kit";
import { age } from "@/ui/format";

export default function WorkerScreen() {
  const { name = "" } = useLocalSearchParams<{ name: string }>();
  const { canWrite } = useAccess();
  const library = useSessionLibrary();
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const [createdID, setCreatedID] = useState("");
  const [title, setTitle] = useState("");
  const [directory, setDirectory] = useState("");
  const [model, setModel] = useState("");
  const queue = useCommandQueue();
  const worker = useWorker(name);
  const sessions = useWorkerSessions(name, offset);
  const created = useCommandStatus(createdID);
  const pull = usePullRefresh(() => Promise.all([worker.refetch(), sessions.refetch()]));
  const { remember } = library;
  const opened = useRef("");
  const createdData = created.data?.id === createdID ? created.data : undefined;
  const newSession = record(createdData?.result).id;
  const creationDone =
    !!createdData &&
    (createdData.status === "done" ||
      createdData.status === "error" ||
      createdData.deliveryUncertain);

  // Follow the create command until OpenCode reports the new session id.
  useEffect(() => {
    if (createdData?.status !== "done" || typeof newSession !== "string") return;
    if (opened.current === createdID) return;
    opened.current = createdID;
    remember({ worker: name, sessionID: newSession, title: title.trim() || "Coordinator session" });
    openSession({ worker: name, sessionID: newSession });
  }, [createdData?.status, newSession, createdID, remember, name, title]);

  if (worker.isLoading)
    return (
      <Screen>
        <Stack.Screen options={{ title: name }} />
        <Loading label="Loading worker" />
      </Screen>
    );
  if (worker.error || !worker.data)
    return (
      <Screen>
        <ErrorState error={worker.error} retry={() => void worker.refetch()} />
      </Screen>
    );
  const data = worker.data;
  const disabled = !canWrite || !usable(data) || queue.isPending;
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: name }} />
      <Heading eyebrow="OpenCode worker">{name}</Heading>
      <Label tone={usable(data) ? "accent" : "danger"} bold>
        {health(data)}
      </Label>
      <Label tone="secondary" size={13}>
        {data.hostname || "Unknown host"} · OpenCode {data.opencode_version || "not seen"} · seen{" "}
        {age(data.last_sync_at)}
      </Label>
      <Label tone="secondary" size={13}>
        {data.counts?.sessions ?? 0} sessions · {data.counts?.pending ?? 0} waiting for input ·{" "}
        {data.dropped_events} dropped events
      </Label>
      {data.last_error ? <Notice error>{data.last_error}</Notice> : null}
      {!usable(data) ? (
        <Notice>
          Remote controls are unavailable. Start or reconnect this worker’s bridge and local
          OpenCode endpoint.
        </Notice>
      ) : null}
      <Button
        kind="primary"
        label="New session"
        disabled={disabled}
        onPress={() => setCreating(true)}
      />
      {createdID && !creationDone ? (
        <Label tone="secondary">
          Creating session… {created.data ? `(${created.data.status})` : ""}
        </Label>
      ) : null}
      {created.data?.status === "error" ? (
        <Notice error>{created.data.error || "Could not create the session"}</Notice>
      ) : null}
      <CommandFeedback queue={queue} />
      {data.pending_interactions?.length ? (
        <Panel>
          <Label tone="muted" size={12} bold>
            WAITING FOR INPUT
          </Label>
          <Row>
            {data.pending_interactions.map((request) => (
              <Button
                key={`${request.kind}-${request.id}`}
                small
                label={`${request.kind} · ${request.sessionID}`}
                onPress={() => openSession({ worker: name, sessionID: request.sessionID })}
              />
            ))}
          </Row>
        </Panel>
      ) : null}
      <Label tone="muted" size={12} bold>
        SESSIONS{sessions.data ? ` · ${sessions.data.count}` : ""}
      </Label>
      {sessions.isLoading ? (
        <Loading label="Loading sessions" />
      ) : sessions.error ? (
        <ErrorState error={sessions.error} retry={() => void sessions.refetch()} />
      ) : !sessions.data?.count ? (
        <Empty title="No sessions observed yet" />
      ) : (
        sessions.data.items.map((item) => (
          <Panel
            key={item.sessionID}
            accessibilityLabel={`Open session ${item.title || item.sessionID}`}
            onPress={() => openSession({ worker: name, sessionID: item.sessionID })}
          >
            <Label bold numberOfLines={2}>
              {item.title || item.sessionID}
            </Label>
            <Label tone={item.counts.pending ? "danger" : "secondary"} size={13}>
              {item.executionStatus || "Unknown"}
              {item.counts.pending > 0 ? ` · ${item.counts.pending} needs input` : ""}
            </Label>
            <Label tone="muted" size={12} numberOfLines={1}>
              {item.directory || "Default workspace"}
            </Label>
            {item.parentID ? (
              <Label tone="muted" size={12}>
                Subagent of {item.parentID}
              </Label>
            ) : null}
          </Panel>
        ))
      )}
      {sessions.data && sessions.data.count > 50 ? (
        <Row style={{ justifyContent: "space-between" }}>
          <Button
            small
            label="Previous"
            disabled={!offset}
            onPress={() => setOffset(Math.max(0, offset - 50))}
          />
          <Label tone="muted" size={13}>
            {offset + 1}–{Math.min(offset + 50, sessions.data.count)} of {sessions.data.count}
          </Label>
          <Button
            small
            label="Next"
            disabled={offset + 50 >= sessions.data.count}
            onPress={() => setOffset(offset + 50)}
          />
        </Row>
      ) : null}
      <Sheet visible={creating} title="New session" onClose={() => setCreating(false)}>
        <Field
          label="Session title"
          maxLength={500}
          value={title}
          onChangeText={setTitle}
          placeholder="Coordinator session"
        />
        <Field
          label="Working directory"
          maxLength={500}
          value={directory}
          onChangeText={setDirectory}
          autoCapitalize="none"
          autoCorrect={false}
          placeholder={data.create_directory || data.directory || "Worker default directory"}
        />
        <ModelPicker worker={data} value={model} onChange={setModel} />
        <Button
          kind="primary"
          label="Create session"
          disabled={disabled}
          onPress={() =>
            queue.mutate(
              {
                worker: name,
                action: "create",
                payload: {
                  title: title.trim() || "Coordinator session",
                  ...(directory.trim() ? { directory: directory.trim() } : {}),
                  ...modelPayload(model, data.models),
                },
              },
              {
                onSuccess: (command) => {
                  setCreatedID(command.id);
                  setCreating(false);
                },
              },
            )
          }
        />
        <CommandFeedback queue={queue} />
      </Sheet>
    </Screen>
  );
}
