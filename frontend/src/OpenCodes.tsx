import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { Monitor, Plus, RefreshCw, Send, Square } from "lucide-react";
import { ApiError, get, post } from "./api";
import { Age, Empty, ErrorState, Loading } from "./ui";
import type { Page } from "./types";
import type {
  CommandInput,
  Interaction,
  MessageWindow,
  ModelChoice,
  RemoteCommand,
  Session,
  Worker,
} from "./opencode-types";
import "./opencodes.css";
import { mergeLiveMessages, useFocusedSession } from "./session-live";

type Navigate = (
  changes: Record<string, string | number | null>,
  push?: boolean,
) => void;
type Draft = { choices: string[][]; custom: string[] };
type Drafts = Record<string, Draft>;
const encoded = encodeURIComponent;
const workerPath = (name: string) => `/api/workers/${encoded(name)}`;
const sessionPath = (worker: string, session: string) =>
  `${workerPath(worker)}/sessions/${encoded(session)}`;
function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
function pretty(value: unknown) {
  return (
    typeof value === "string" ? value : (JSON.stringify(value, null, 2) ?? "")
  ).slice(0, 20000);
}
function usable(worker: Worker) {
  return (
    worker.is_enabled &&
    worker.online &&
    worker.connected &&
    worker.stream_connected
  );
}
function health(worker: Worker) {
  return !worker.is_enabled
    ? "Disabled"
    : !worker.online
      ? "Bridge offline"
      : !worker.connected
        ? "OpenCode unreachable"
        : !worker.stream_connected
          ? "Event stream reconnecting"
          : "Connected";
}
function modelPayload(value: string, models: ModelChoice[]) {
  const selected = models.find(
    (model) => `${model.providerID}/${model.modelID}` === value,
  );
  return selected
    ? { model: { providerID: selected.providerID, modelID: selected.modelID } }
    : {};
}
function ModelSelect({
  value,
  change,
  worker,
}: {
  value: string;
  change: (value: string) => void;
  worker: Worker;
}) {
  return (
    <label>
      Model
      <select
        aria-label="OpenCode model"
        value={value}
        onChange={(event) => change(event.target.value)}
      >
        <option value="">
          Worker default
          {worker.default_model ? ` · ${worker.default_model.modelID}` : ""}
        </option>
        {worker.models.map((model) => (
          <option
            key={`${model.providerID}/${model.modelID}`}
            value={`${model.providerID}/${model.modelID}`}
          >
            {model.providerID} / {model.name || model.modelID}
          </option>
        ))}
      </select>
    </label>
  );
}
function useCommandQueue() {
  const client = useQueryClient();
  // Keep the same idempotency key for an unchanged request after a lost response.
  const intents = useRef(new Map<string, string>());
  return useMutation({
    retry: false,
    mutationFn: (input: CommandInput) => {
      const body = JSON.stringify(input);
      const key = intents.current.get(body) || crypto.randomUUID();
      intents.current.set(body, key);
      return post<RemoteCommand>("/api/commands", {
        ...input,
        client_request_id: key,
      });
    },
    onSuccess: (_data, input) => {
      intents.current.delete(JSON.stringify(input));
      void client.invalidateQueries({ queryKey: ["opencodes"] });
    },
    onError: () => {
      void client.invalidateQueries({ queryKey: ["opencodes"] });
    },
  });
}
type Queue = ReturnType<typeof useCommandQueue>;
function Feedback({ queue }: { queue: Queue }) {
  return (
    <div className="oc-feedback" aria-live="polite">
      {queue.isPending && "Submitting command…"}
      {queue.error && (
        <p role="alert">
          {queue.error.message}
          {!(queue.error instanceof ApiError) &&
            " Delivery may already be queued. Retrying the unchanged request reuses its idempotency key."}
        </p>
      )}
      {queue.isSuccess && (
        <span>
          Command #{queue.data.id} queued. Delivery and execution status appear
          below.
        </span>
      )}
    </div>
  );
}
function Pager({
  offset,
  count,
  change,
  size = 50,
}: {
  offset: number;
  count: number;
  change: (offset: number) => void;
  size?: number;
}) {
  return count > size ? (
    <div className="oc-pager">
      <button
        className="button secondary"
        disabled={!offset}
        onClick={() => change(Math.max(0, offset - size))}
      >
        Previous
      </button>
      <span>
        {offset + 1}–{Math.min(offset + size, count)} of {count}
      </span>
      <button
        className="button secondary"
        disabled={offset + size >= count}
        onClick={() => change(offset + size)}
      >
        Next
      </button>
    </div>
  ) : null;
}

export default function OpenCodes({
  worker,
  session,
  navigate,
  openTask,
}: {
  worker: string;
  session: string;
  navigate: Navigate;
  openTask: (id: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [drafts, setDrafts] = useState<Drafts>({});
  const client = useQueryClient();
  const workers = useQuery({
    queryKey: ["opencodes", "workers", offset],
    queryFn: () => get<Page<Worker>>(`/api/workers?limit=50&offset=${offset}`),
    refetchInterval: 10000,
  });
  const name = worker || workers.data?.items[0]?.name || "";
  const selectWorker = (value: string) =>
    navigate({ section: "opencodes", worker: value, session: null }, true);
  const selectSession = (value: string) =>
    navigate({ section: "opencodes", worker: name, session: value }, true);
  return (
    <section className="oc-workspace" aria-label="Connected OpenCodes">
      <div className="oc-heading">
        <div>
          <div className="eyebrow">CENTRAL WORKER CONSOLE</div>
          <h1>
            Connected OpenCodes<span className="heading-dot">.</span>
          </h1>
          <p>Live sessions from your machines. Coordinated here.</p>
        </div>
        <button
          className="button secondary"
          onClick={() =>
            void client.invalidateQueries({ queryKey: ["opencodes"] })
          }
        >
          <RefreshCw size={14} />
          Refresh
        </button>
      </div>
      {workers.isLoading ? (
        <Loading />
      ) : workers.error ? (
        <ErrorState
          error={workers.error}
          retry={() => void workers.refetch()}
        />
      ) : workers.data?.count === 0 ? (
        <Empty
          title="No OpenCodes registered"
          description="Provision a worker and run its outbound bridge with this coordinator URL and its bound agent key."
        >
          <a
            className="button secondary"
            href="https://github.com/aaronx3011/agents-task-manager/issues/3"
            target="_blank"
            rel="noreferrer"
          >
            Worker setup guide
          </a>
        </Empty>
      ) : (
        <div className="oc-layout">
          <aside className="oc-workers" aria-label="OpenCode workers">
            <h2>
              <Monitor size={15} />
              Workers <span>{workers.data?.count}</span>
            </h2>
            {workers.data?.items.map((item) => (
              <button
                key={item.name}
                className={`oc-worker ${name === item.name ? "selected" : ""}`}
                aria-pressed={name === item.name}
                onClick={() => selectWorker(item.name)}
              >
                <strong>{item.name}</strong>
                <span className={usable(item) ? "oc-online" : "oc-warning"}>
                  {health(item)}
                </span>
                <small>{item.hostname || "Waiting for first sync"}</small>
                <small>
                  Seen <Age value={item.last_sync_at} />
                </small>
              </button>
            ))}
            <Pager
              offset={offset}
              count={workers.data?.count || 0}
              change={setOffset}
            />
          </aside>
          {name && (
            <WorkerPanel
              key={name}
              name={name}
              selectedSession={session}
              selectSession={selectSession}
              openTask={openTask}
              drafts={drafts}
              setDraft={(key, draft) =>
                setDrafts((previous) => ({ ...previous, [key]: draft }))
              }
            />
          )}
        </div>
      )}
    </section>
  );
}

function WorkerPanel({
  name,
  selectedSession,
  selectSession,
  openTask,
  drafts,
  setDraft,
}: {
  name: string;
  selectedSession: string;
  selectSession: (session: string) => void;
  openTask: (id: number) => void;
  drafts: Drafts;
  setDraft: (key: string, draft: Draft) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const [createdID, setCreatedID] = useState("");
  const [title, setTitle] = useState("");
  const [directory, setDirectory] = useState("");
  const [model, setModel] = useState("");
  const queue = useCommandQueue();
  const worker = useQuery({
    queryKey: ["opencodes", name, "detail"],
    queryFn: () => get<Worker>(workerPath(name)),
    refetchInterval: 10000,
  });
  const sessions = useQuery({
    queryKey: ["opencodes", name, "sessions", offset],
    queryFn: () =>
      get<Page<Session>>(
        `${workerPath(name)}/sessions?limit=50&offset=${offset}`,
      ),
    refetchInterval: 5000,
  });
  const commands = useQuery({
    queryKey: ["opencodes", name, "commands"],
    queryFn: () =>
      get<Page<RemoteCommand>>(
        `/api/commands?worker=${encoded(name)}&limit=30`,
      ),
    refetchInterval: 10000,
  });
  const created = commands.data?.items.find(
    (command) => command.id === createdID,
  );
  useEffect(() => {
    const id = record(created?.result).id;
    if (created?.status === "done" && typeof id === "string") {
      selectSession(id);
      setCreatedID("");
    }
  }, [created, selectSession]);
  if (worker.isLoading) return <Loading />;
  if (worker.error)
    return (
      <ErrorState error={worker.error} retry={() => void worker.refetch()} />
    );
  const data = worker.data!;
  const current = selectedSession || sessions.data?.items[0]?.sessionID || "";
  const disabled = !usable(data) || queue.isPending;
  return (
    <div className="oc-worker-panel">
      <header className="oc-worker-header">
        <div>
          <h2>{name}</h2>
          <p>
            <span className={usable(data) ? "oc-online" : "oc-warning"}>
              {health(data)}
            </span>{" "}
            · {data.hostname || "Unknown host"} · OpenCode{" "}
            {data.opencode_version || "not seen"}
          </p>
          <p>
            {data.counts?.sessions ?? 0} sessions · {data.counts?.pending ?? 0}{" "}
            waiting for input · {data.dropped_events} dropped events
          </p>
        </div>
        <button
          className="button secondary"
          disabled={disabled}
          onClick={() => setCreating(!creating)}
        >
          <Plus size={14} />
          New session
        </button>
      </header>
      {data.last_error && (
        <p role="alert" className="oc-alert">
          {data.last_error}
        </p>
      )}
      {!usable(data) && (
        <p className="oc-alert">
          Remote controls are unavailable. Start or reconnect this worker’s
          bridge and local OpenCode endpoint.
        </p>
      )}
      {creating && (
        <form
          className="oc-create oc-form"
          onSubmit={(event) => {
            event.preventDefault();
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
            );
          }}
        >
          <label>
            Session title
            <input
              aria-label="New session title"
              maxLength={500}
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Coordinator session"
            />
          </label>
          <label>
            Working directory
            <input
              aria-label="Working directory"
              maxLength={500}
              value={directory}
              onChange={(event) => setDirectory(event.target.value)}
              placeholder={
                data.create_directory ||
                data.directory ||
                "Worker default directory"
              }
            />
          </label>
          <ModelSelect worker={data} value={model} change={setModel} />
          <button className="button secondary" disabled={disabled}>
            Create session
          </button>
        </form>
      )}
      <Feedback queue={queue} />
      {Boolean(data.pending_interactions?.length) && (
        <div className="oc-pending-summary">
          Waiting for input:{" "}
          {data.pending_interactions?.map((request) => (
            <button
              className="button secondary"
              key={`${request.kind}-${request.id}`}
              onClick={() => selectSession(request.sessionID)}
            >
              {request.kind} · {request.sessionID}
            </button>
          ))}
        </div>
      )}
      <div className="oc-session-layout">
        <aside className="oc-sessions" aria-label="OpenCode sessions">
          <h3>Sessions</h3>
          {sessions.isLoading ? (
            <Loading />
          ) : sessions.error ? (
            <ErrorState
              error={sessions.error}
              retry={() => void sessions.refetch()}
            />
          ) : !sessions.data?.count ? (
            <Empty title="No sessions observed yet" />
          ) : (
            sessions.data.items.map((item) => (
              <button
                className={`oc-session ${current === item.sessionID ? "selected" : ""}`}
                key={item.sessionID}
                aria-pressed={current === item.sessionID}
                onClick={() => selectSession(item.sessionID)}
              >
                <strong>{item.title || item.sessionID}</strong>
                <span>
                  {item.executionStatus || "Unknown"}
                  {item.counts.pending > 0
                    ? ` · ${item.counts.pending} needs input`
                    : ""}
                </span>
                <small title={item.directory}>
                  {item.directory || "Default workspace"}
                </small>
                {item.parentID && <small>Subagent of {item.parentID}</small>}
              </button>
            ))
          )}
          <Pager
            offset={offset}
            count={sessions.data?.count || 0}
            change={setOffset}
          />
        </aside>
        {current ? (
          <SessionPanel
            key={current}
            worker={data}
            sessionID={current}
            queue={queue}
            selectSession={selectSession}
            openTask={openTask}
            drafts={drafts}
            setDraft={setDraft}
          />
        ) : (
          <Empty title="Choose or create a session" />
        )}
      </div>
      <details className="oc-commands" open>
        <summary>
          Recent worker commands · {commands.data?.count ?? "—"}
        </summary>
        {commands.error ? (
          <ErrorState
            error={commands.error}
            retry={() => void commands.refetch()}
          />
        ) : (
          commands.data?.items.map((command) => (
            <CommandCard
              key={command.id}
              command={command}
              selectSession={selectSession}
            />
          ))
        )}
        {(commands.data?.count || 0) > 30 && (
          <p className="muted">
            Showing the 30 latest commands. Full history is retained by the
            coordinator API.
          </p>
        )}
      </details>
    </div>
  );
}

function CommandCard({
  command,
  selectSession,
}: {
  command: RemoteCommand;
  selectSession: (session: string) => void;
}) {
  const label = command.deliveryUncertain
    ? "Delivery uncertain"
    : command.status === "done" && command.action === "prompt"
      ? "Accepted · execution separate"
      : command.status;
  return (
    <div
      className={`oc-command ${command.deliveryUncertain || command.status === "error" ? "oc-warning" : ""}`}
    >
      <strong>
        #{command.id} · {command.action}
      </strong>
      <span>{label}</span>
      <Age value={command.createdAt} />
      {command.sessionID && (
        <button
          className="oc-text-button"
          onClick={() => selectSession(command.sessionID!)}
        >
          Open session
        </button>
      )}
      {command.error && <p>{command.error}</p>}
      {command.deliveryUncertain && (
        <p>
          {command.uncertain ||
            "Inspect session history and reconcile this command through the staff API. It will not be replayed automatically."}
        </p>
      )}
    </div>
  );
}

function SessionPanel({
  worker,
  sessionID,
  queue,
  selectSession,
  openTask,
  drafts,
  setDraft,
}: {
  worker: Worker;
  sessionID: string;
  queue: Queue;
  selectSession: (session: string) => void;
  openTask: (id: number) => void;
  drafts: Drafts;
  setDraft: (key: string, draft: Draft) => void;
}) {
  const [text, setText] = useState("");
  const [model, setModel] = useState("");
  const [noReply, setNoReply] = useState(false);
  const [cursors, setCursors] = useState<number[]>([]);
  const after = cursors.at(-1);
  const live = useFocusedSession(worker.name, sessionID);
  const path = sessionPath(worker.name, sessionID);
  const detail = useQuery({
    queryKey: ["opencodes", worker.name, sessionID, "detail"],
    queryFn: () => get<Session>(path),
    refetchInterval: 5000,
  });
  const messages = useQuery({
    queryKey: ["opencodes", worker.name, sessionID, "messages", after],
    queryFn: () =>
      get<MessageWindow>(
        `${path}/messages?limit=500${after === undefined ? "" : `&after=${after}`}`,
      ),
    refetchInterval: live ? 15000 : 5000,
  });
  const disabled = !usable(worker) || queue.isPending;
  const displayedMessages = mergeLiveMessages(
    messages.data?.messages || [],
    after === undefined ? live : null,
  );
  if (detail.isLoading) return <Loading />;
  if (detail.error)
    return (
      <ErrorState error={detail.error} retry={() => void detail.refetch()} />
    );
  const data = detail.data!;
  return (
    <section className="oc-conversation" aria-label="OpenCode conversation">
      <header>
        <h2>{data.title || sessionID}</h2>
        <p>
          {live?.executionStatus || data.executionStatus || "Unknown state"} ·{" "}
          {data.agentName || "Default agent"}
          {data.model
            ? ` · ${data.model.providerID}/${data.model.modelID}`
            : ""}
        </p>
        <code>{sessionID}</code>
        <p className="oc-directory">{data.directory}</p>
        <p className="muted">
          {live
            ? "Live conversation · batched storage"
            : "Saved history · waiting for live updates"}
        </p>
        {data.parentID && (
          <button
            className="oc-text-button"
            onClick={() => selectSession(data.parentID!)}
          >
            Parent session {data.parentID}
          </button>
        )}
        {data.subagents?.map((id) => (
          <button
            key={id}
            className="oc-text-button"
            onClick={() => selectSession(id)}
          >
            Subagent {id}
          </button>
        ))}
        {data.taskLinks?.map((id) => (
          <button
            key={id}
            className="button secondary"
            onClick={() => openTask(id)}
          >
            Task #{id}
          </button>
        ))}
        <button
          className="button secondary"
          disabled={disabled}
          onClick={() =>
            queue.mutate({
              worker: worker.name,
              action: "interrupt",
              sessionID,
              payload: {},
            })
          }
        >
          <Square size={12} />
          Interrupt
        </button>
      </header>
      {data.lastError && (
        <p role="alert" className="oc-alert">
          {data.lastError}
        </p>
      )}
      {data.pendingInteractions?.map((request) => {
        const key = `${worker.name}/${sessionID}/${request.kind}/${request.id}`;
        const reply = data.commands?.find(
          (command) => command.id === request.replyCommandId,
        );
        const locked =
          disabled ||
          (request.replyDispatched &&
            (reply?.status !== "error" || Boolean(reply.deliveryUncertain)));
        return (
          <InteractionForm
            key={key}
            request={request}
            worker={worker.name}
            queue={queue}
            disabled={locked}
            draft={drafts[key] || { choices: [], custom: [] }}
            change={(draft) => setDraft(key, draft)}
          />
        );
      })}
      <div className="oc-messages" aria-live="off">
        {messages.isLoading ? (
          <Loading />
        ) : messages.error ? (
          <ErrorState
            error={messages.error}
            retry={() => void messages.refetch()}
          />
        ) : (
          <>
            <p className="oc-window">
              Retained observation window · {displayedMessages.length} messages
              ·{" "}
              {data.window.complete
                ? "Complete supplied window"
                : "History may be partial"}
              {messages.data?.gap ? " · Cursor gap: older data was pruned" : ""}
            </p>
            {displayedMessages.map((message) => (
              <article
                className={`oc-message oc-role-${message.role}`}
                key={message.remoteID}
              >
                <header>
                  <strong>{message.role || "message"}</strong>
                  <Age value={message.publishedAt} />
                </header>
                {Boolean(message.content.error) && (
                  <pre role="alert">{pretty(message.content.error)}</pre>
                )}
                {message.parts.map((part, index) => (
                  <MessagePart key={index} value={part} />
                ))}
              </article>
            ))}
            {!displayedMessages.length && (
              <Empty title="No messages observed yet" />
            )}
            {(cursors.length > 0 || messages.data?.hasMore) && (
              <div className="oc-pager">
                <button
                  className="button secondary"
                  disabled={!cursors.length}
                  onClick={() => setCursors((values) => values.slice(0, -1))}
                >
                  Previous messages
                </button>
                <button
                  className="button secondary"
                  disabled={!messages.data?.hasMore}
                  onClick={() =>
                    setCursors((values) => [...values, messages.data!.cursor])
                  }
                >
                  Next messages
                </button>
              </div>
            )}
          </>
        )}
      </div>
      <form
        className="oc-compose oc-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (text.trim())
            queue.mutate(
              {
                worker: worker.name,
                action: "prompt",
                sessionID,
                payload: {
                  text: text.trim(),
                  noReply,
                  ...modelPayload(model, worker.models),
                },
              },
              { onSuccess: () => setText("") },
            );
        }}
      >
        <label>
          Message
          <textarea
            aria-label="Message to OpenCode"
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={3}
            maxLength={20000}
            placeholder="Send instructions to this OpenCode session…"
          />
        </label>
        <div className="oc-compose-actions">
          <ModelSelect worker={worker} value={model} change={setModel} />
          <label className="oc-inline">
            <input
              type="checkbox"
              checked={noReply}
              onChange={(event) => setNoReply(event.target.checked)}
            />
            Record only (no model reply)
          </label>
          <button
            className="button secondary"
            disabled={disabled || !text.trim()}
          >
            <Send size={14} />
            Send message
          </button>
        </div>
        <p className="muted">
          A delivered prompt is accepted input, not proof of model or task
          completion. Idle is local to this endpoint.
        </p>
      </form>
    </section>
  );
}

function MessagePart({ value }: { value: unknown }) {
  const part = record(value);
  if (part.type === "text" && typeof part.text === "string")
    return (
      <div className="oc-markdown">
        <ReactMarkdown skipHtml>{part.text}</ReactMarkdown>
      </div>
    );
  if (part.type === "reasoning")
    return (
      <details>
        <summary>Reasoning</summary>
        <pre>{pretty(part.text)}</pre>
      </details>
    );
  if (part.type === "tool") {
    const state = record(part.state);
    return (
      <details>
        <summary>
          Tool · {String(part.tool || "unknown")} ·{" "}
          {String(state.status || "unknown")}
        </summary>
        <h4>Input</h4>
        <pre>{pretty(state.input)}</pre>
        {state.output !== undefined && (
          <>
            <h4>Output</h4>
            <pre>{pretty(state.output)}</pre>
          </>
        )}
        {state.error !== undefined && (
          <pre role="alert">{pretty(state.error)}</pre>
        )}
      </details>
    );
  }
  return (
    <details>
      <summary>{String(part.type || "Message part")}</summary>
      <pre>{pretty(part)}</pre>
    </details>
  );
}

function InteractionForm({
  request,
  worker,
  queue,
  disabled,
  draft,
  change,
}: {
  request: Interaction;
  worker: string;
  queue: Queue;
  disabled: boolean;
  draft: Draft;
  change: (draft: Draft) => void;
}) {
  const send = (action: string, payload: Record<string, unknown>) =>
    queue.mutate({
      worker,
      action,
      sessionID: request.sessionID,
      payload: { requestID: request.id, ...payload },
    });
  if (request.kind === "permission")
    return (
      <section className="oc-interaction" aria-label="Pending permission">
        <h3>{request.title || request.type || "Permission required"}</h3>
        <p>{request.description}</p>
        {Boolean(request.patterns) && <pre>{pretty(request.patterns)}</pre>}
        <div className="oc-compose-actions">
          <button
            className="button secondary"
            disabled={disabled}
            onClick={() => send("permission.reply", { reply: "once" })}
          >
            Allow once
          </button>
          <button
            className="button secondary"
            disabled={disabled}
            onClick={() => send("permission.reply", { reply: "always" })}
          >
            Always allow matching rules
          </button>
          <button
            className="button secondary"
            disabled={disabled}
            onClick={() => send("permission.reply", { reply: "reject" })}
          >
            Deny
          </button>
        </div>
        {disabled && request.replyDispatched && (
          <p>Response recorded; awaiting worker reconciliation.</p>
        )}
      </section>
    );
  const answers = (request.questions || []).map((question, index) => {
    const selected = draft.choices[index] || [];
    const custom = draft.custom[index]?.trim();
    return custom && question.custom
      ? question.multiple
        ? [...selected, custom]
        : [custom]
      : selected;
  });
  return (
    <form
      className="oc-interaction oc-form"
      aria-label="Pending questions"
      onSubmit={(event) => {
        event.preventDefault();
        send("question.reply", { answers });
      }}
    >
      <h3>Answers needed</h3>
      {request.questions?.map((question, index) => (
        <fieldset key={index} disabled={disabled}>
          <legend>{question.header || `Question ${index + 1}`}</legend>
          <p>{question.text}</p>
          {question.options.map((option) => (
            <label className="oc-choice" key={option.label}>
              <input
                type={question.multiple ? "checkbox" : "radio"}
                name={`${worker}-${request.id}-${index}`}
                checked={
                  (draft.choices[index] || []).includes(option.label) &&
                  (question.multiple || !draft.custom[index])
                }
                onChange={(event) => {
                  const choices = [...draft.choices];
                  choices[index] = question.multiple
                    ? event.target.checked
                      ? [...(choices[index] || []), option.label]
                      : (choices[index] || []).filter(
                          (value) => value !== option.label,
                        )
                    : [option.label];
                  const custom = [...draft.custom];
                  if (!question.multiple) custom[index] = "";
                  change({ choices, custom });
                }}
              />
              <span>
                {option.label}
                <small>{option.description}</small>
              </span>
            </label>
          ))}
          {question.custom && (
            <label>
              Custom answer
              <input
                aria-label={`Custom answer ${index + 1}`}
                maxLength={200}
                value={draft.custom[index] || ""}
                onChange={(event) => {
                  const custom = [...draft.custom];
                  custom[index] = event.target.value;
                  change({ ...draft, custom });
                }}
              />
            </label>
          )}
        </fieldset>
      ))}
      <div className="oc-compose-actions">
        <button
          className="button secondary"
          disabled={
            disabled ||
            !answers.length ||
            answers.some((answer) => !answer.length)
          }
        >
          Submit answers
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={disabled}
          onClick={() => send("question.reject", {})}
        >
          Dismiss questions
        </button>
      </div>
      {disabled && request.replyDispatched && (
        <p>Response recorded; awaiting worker reconciliation.</p>
      )}
    </form>
  );
}
