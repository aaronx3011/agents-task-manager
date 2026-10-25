import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import {
  ArrowDown,
  Monitor,
  Pin,
  Plus,
  RefreshCw,
  Send,
  Square,
} from "lucide-react";
import { ApiError, get, post } from "./api";
import { Age, Empty, ErrorState, Loading } from "./ui";
import type { Page } from "./types";
import type {
  CommandInput,
  Interaction,
  MessageWindow,
  Message,
  ModelChoice,
  RemoteCommand,
  Session,
  Worker,
} from "./opencode-types";
import "./opencodes.css";
import { mergeLiveMessages, useFocusedSession } from "./session-live";
import { sessionIdentity } from "./session-library";
import type { SessionLibrary, SessionReference } from "./session-library";

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
  mode,
  library,
}: {
  worker: string;
  session: string;
  navigate: Navigate;
  openTask: (id: number) => void;
  mode: "workers" | "sessions";
  library: SessionLibrary;
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
    navigate(
      {
        section: mode === "sessions" ? "sessions" : "opencodes",
        worker: value,
        session: null,
      },
      true,
    );
  const selectSession = (value: string) =>
    navigate(
      {
        section: mode === "sessions" ? "sessions" : "opencodes",
        worker: name,
        session: value,
      },
      true,
    );
  return (
    <section
      className="oc-workspace"
      aria-label={
        mode === "sessions" ? "Sessions workspace" : "Connected OpenCodes"
      }
    >
      <div className="oc-heading">
        <div>
          <div className="eyebrow">CENTRAL WORKER CONSOLE</div>
          <h1>
            {mode === "sessions" ? "Sessions" : "Connected OpenCodes"}
            <span className="heading-dot">.</span>
          </h1>
          <p>
            {mode === "sessions"
              ? "Pinned, recently opened and new conversations. Loaded only when selected."
              : "Live sessions from your machines. Coordinated here."}
          </p>
          {mode === "sessions" && (
            <label className="oc-worker-choice">
              Worker for new sessions{" "}
              <select
                aria-label="Worker for new sessions"
                value={name}
                onChange={(event) => selectWorker(event.target.value)}
              >
                {workers.data?.items.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
          )}
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
          {mode === "sessions" ? (
            <SessionShelf
              library={library}
              worker={name}
              session={session}
              select={(item) => {
                library.remember(item);
                navigate(
                  {
                    section: "sessions",
                    worker: item.worker,
                    session: item.sessionID,
                  },
                  true,
                );
              }}
            />
          ) : (
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
          )}
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
              library={library}
              shelf={mode === "sessions"}
            />
          )}
        </div>
      )}
    </section>
  );
}

function SessionShelf({
  library,
  worker,
  session,
  select,
}: {
  library: SessionLibrary;
  worker: string;
  session: string;
  select: (item: SessionReference) => void;
}) {
  const [offset, setOffset] = useState(0);
  const recent = useQuery({
    queryKey: ["opencodes", "recent-sessions", offset],
    queryFn: ({ signal }) =>
      get<Page<Session>>(
        `/api/sessions?ordering=recent&limit=20&offset=${offset}`,
        signal,
      ),
    refetchInterval: 5000,
  });
  const saved = new Set(library.entries.map(sessionIdentity));
  const groups = [
    { title: "Pinned", items: library.entries.filter((item) => item.pinned) },
    {
      title: "Recently focused",
      items: library.entries.filter((item) => !item.pinned),
    },
    {
      title: "New and recent",
      items: (recent.data?.items || []).filter(
        (item) => !saved.has(sessionIdentity(item)),
      ),
    },
  ];
  return (
    <aside
      className="oc-workers oc-session-shelf"
      aria-label="Saved and new sessions"
    >
      {groups.map((group) => (
        <section key={group.title} aria-label={group.title}>
          <h3>{group.title}</h3>
          {group.items.map((item) => {
            const current =
              recent.data?.items.find(
                (candidate) =>
                  sessionIdentity(candidate) === sessionIdentity(item),
              ) || item;
            const pinned = library.entries.some(
              (entry) =>
                entry.pinned &&
                sessionIdentity(entry) === sessionIdentity(item),
            );
            return (
              <div className="oc-shelf-item" key={sessionIdentity(item)}>
                <button
                  className={`oc-session ${worker === item.worker && session === item.sessionID ? "selected" : ""}`}
                  onClick={() => select(current)}
                >
                  <strong>{current.title || item.sessionID}</strong>
                  <small>{item.worker}</small>
                </button>
                <button
                  className={`icon-button ${pinned ? "selected" : ""}`}
                  aria-label={`${pinned ? "Unpin" : "Pin"} ${current.title || item.sessionID}`}
                  aria-pressed={pinned}
                  onClick={() => library.togglePin(current)}
                >
                  <Pin size={13} />
                </button>
              </div>
            );
          })}
          {!group.items.length && (
            <p className="muted">
              {group.title === "Pinned"
                ? "Pin a conversation to keep it here."
                : "No sessions yet."}
            </p>
          )}
        </section>
      ))}
      {recent.error && (
        <ErrorState error={recent.error} retry={() => void recent.refetch()} />
      )}
      <Pager
        offset={offset}
        count={recent.data?.count || 0}
        change={setOffset}
        size={20}
      />
    </aside>
  );
}

function WorkerPanel({
  name,
  selectedSession,
  selectSession,
  openTask,
  drafts,
  setDraft,
  library,
  shelf,
}: {
  name: string;
  selectedSession: string;
  selectSession: (session: string) => void;
  openTask: (id: number) => void;
  drafts: Drafts;
  setDraft: (key: string, draft: Draft) => void;
  library: SessionLibrary;
  shelf: boolean;
}) {
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const [createdID, setCreatedID] = useState("");
  const [title, setTitle] = useState("");
  const [directory, setDirectory] = useState("");
  const [model, setModel] = useState("");
  const queue = useCommandQueue();
  const remember = library.remember;
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
    enabled: !shelf,
  });
  const created = useQuery({
    queryKey: ["opencodes", name, "creation", createdID],
    queryFn: ({ signal }) =>
      get<RemoteCommand>(`/api/commands/${createdID}`, signal),
    enabled: Boolean(createdID),
    refetchInterval: 1000,
  });
  useEffect(() => {
    const id = record(created.data?.result).id;
    if (created.data?.status === "done" && typeof id === "string") {
      remember({ worker: name, sessionID: id, title });
      selectSession(id);
      setCreatedID("");
    }
    if (created.data?.status === "error" || created.data?.deliveryUncertain)
      setCreatedID("");
  }, [created.data, selectSession, remember, name, title]);
  if (worker.isLoading) return <Loading />;
  if (worker.error)
    return (
      <ErrorState error={worker.error} retry={() => void worker.refetch()} />
    );
  const data = worker.data!;
  const current = selectedSession;
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
      <div className={`oc-session-layout ${shelf ? "oc-single-session" : ""}`}>
        {!shelf && (
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
        )}
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
            library={library}
          />
        ) : (
          <Empty title="Choose or create a session" />
        )}
      </div>
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
  library,
}: {
  worker: Worker;
  sessionID: string;
  queue: Queue;
  selectSession: (session: string) => void;
  openTask: (id: number) => void;
  drafts: Drafts;
  setDraft: (key: string, draft: Draft) => void;
  library: SessionLibrary;
}) {
  const [text, setText] = useState("");
  const [model, setModel] = useState("");
  const [noReply, setNoReply] = useState(false);
  const [history, setHistory] = useState<{
    messages: Message[];
    cursor: string | null;
    gap: boolean;
  } | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<Error | null>(null);
  const [following, setFollowing] = useState(true);
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const lastTop = useRef(0);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const olderRequest = useRef<AbortController | null>(null);
  const live = useFocusedSession(worker.name, sessionID);
  const path = sessionPath(worker.name, sessionID);
  const detail = useQuery({
    queryKey: ["opencodes", worker.name, sessionID, "detail"],
    queryFn: ({ signal }) => get<Session>(`${path}?compact=true`, signal),
    refetchInterval: 5000,
  });
  const messages = useQuery({
    queryKey: ["opencodes", worker.name, sessionID, "messages", "latest"],
    queryFn: ({ signal }) =>
      get<MessageWindow>(`${path}/messages?latest=true&limit=10`, signal),
    refetchInterval: live ? 15000 : 5000,
    gcTime: library.entries.some(
      (item) =>
        item.pinned &&
        item.worker === worker.name &&
        item.sessionID === sessionID,
    )
      ? 1800000
      : 0,
  });
  const disabled = !usable(worker) || queue.isPending;
  const displayedMessages = mergeLiveMessages(
    [...(history?.messages || []), ...(messages.data?.messages || [])],
    live,
  );
  const cursor = history ? history.cursor : messages.data?.olderCursor;
  const title = detail.data?.title || sessionID;
  const observed = Boolean(detail.data);
  const remember = library.remember;
  const pinned = library.entries.some(
    (item) =>
      item.pinned &&
      item.worker === worker.name &&
      item.sessionID === sessionID,
  );
  useEffect(() => {
    if (observed) remember({ worker: worker.name, sessionID, title });
  }, [remember, worker.name, sessionID, title, observed]);
  useEffect(() => () => olderRequest.current?.abort(), []);
  async function loadOlder() {
    if (!cursor || olderRequest.current) return;
    follow.current = false;
    setFollowing(false);
    const controller = new AbortController();
    olderRequest.current = controller;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const page = await get<MessageWindow>(
        `${path}/messages?latest=true&limit=10&before=${encoded(cursor)}`,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (viewport.current)
        anchor.current = {
          height: viewport.current.scrollHeight,
          top: viewport.current.scrollTop,
        };
      setHistory((previous) => ({
        messages: mergeLiveMessages(
          [
            ...page.messages,
            ...(previous?.messages || []),
            ...(messages.data?.messages || []),
          ],
          null,
        ),
        cursor: page.olderCursor || null,
        gap: Boolean(previous?.gap || page.gap),
      }));
    } catch (error) {
      if (!controller.signal.aborted)
        setOlderError(
          error instanceof Error
            ? error
            : new Error("History could not be loaded"),
        );
    } finally {
      if (!controller.signal.aborted) setLoadingOlder(false);
      olderRequest.current = null;
    }
  }
  useLayoutEffect(() => {
    const node = viewport.current;
    if (!node) return;
    if (anchor.current) {
      node.scrollTop =
        anchor.current.top + node.scrollHeight - anchor.current.height;
      anchor.current = null;
    } else if (follow.current) node.scrollTop = node.scrollHeight;
    lastTop.current = node.scrollTop;
  }, [displayedMessages, detail.data]);
  useEffect(() => {
    if (!content.current || !viewport.current) return;
    const observer = new ResizeObserver(() => {
      if (follow.current && viewport.current) {
        viewport.current.scrollTop = viewport.current.scrollHeight;
        lastTop.current = viewport.current.scrollTop;
      }
    });
    observer.observe(content.current);
    return () => observer.disconnect();
  }, [detail.data?.sessionID]);
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
        <button
          className={`button secondary ${pinned ? "selected" : ""}`}
          aria-pressed={pinned}
          onClick={() =>
            library.togglePin({ worker: worker.name, sessionID, title })
          }
        >
          <Pin size={13} />
          {pinned ? "Unpin session" : "Pin session"}
        </button>
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
      <div
        className="oc-messages"
        aria-live="off"
        aria-label="Conversation messages"
        ref={viewport}
        onScroll={(event) => {
          const node = event.currentTarget;
          const nearBottom =
            node.scrollHeight - node.scrollTop - node.clientHeight < 80;
          follow.current = nearBottom;
          setFollowing(nearBottom);
          if (node.scrollTop < 80 && node.scrollTop < lastTop.current)
            void loadOlder();
          lastTop.current = node.scrollTop;
        }}
      >
        <div ref={content}>
          {messages.isLoading ? (
            <Loading />
          ) : messages.error ? (
            <ErrorState
              error={messages.error}
              retry={() => void messages.refetch()}
            />
          ) : (
            <>
              {cursor && (
                <button
                  className="oc-text-button oc-load-older"
                  disabled={loadingOlder}
                  onClick={() => void loadOlder()}
                >
                  {loadingOlder
                    ? "Loading older messages…"
                    : "Load older messages"}
                </button>
              )}
              {olderError && (
                <ErrorState error={olderError} retry={() => void loadOlder()} />
              )}
              <p className="oc-window">
                Retained observation window · {displayedMessages.length}{" "}
                messages ·{" "}
                {data.window.complete
                  ? "Complete supplied window"
                  : "History may be partial"}
                {messages.data?.gap || history?.gap
                  ? " · Cursor gap: older data was pruned"
                  : ""}
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
              {!cursor && displayedMessages.length > 0 && (
                <p className="oc-window">Oldest retained messages reached.</p>
              )}
            </>
          )}
        </div>
      </div>
      {!following && (
        <button
          className="button secondary oc-jump-latest"
          onClick={() => {
            follow.current = true;
            setFollowing(true);
            if (viewport.current)
              viewport.current.scrollTop = viewport.current.scrollHeight;
          }}
        >
          <ArrowDown size={13} />
          Latest messages
        </button>
      )}
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
