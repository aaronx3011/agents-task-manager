import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowDownWideNarrow,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Command,
  Layers3,
  LayoutGrid,
  List,
  Moon,
  PanelRightClose,
  PanelRightOpen,
  Radio,
  Search,
  Settings,
  Monitor,
  SlidersHorizontal,
  Sun,
  Users,
  X,
} from "lucide-react";
import { ApiError, get, query } from "./api";
import { Board } from "./Board";
import { useLiveUpdates } from "./live";
import { useLocationState } from "./location";
import { TaskList } from "./TaskList";
import { ModalBoundary } from "./ModalBoundary";
import { LABELS, STATUSES } from "./types";
import type { Agent, Workspace } from "./types";
import {
  Age,
  Avatar,
  ClockProvider,
  Empty,
  ErrorState,
  Loading,
  tone,
  useNow,
} from "./ui";

const TaskModal = lazy(() =>
  import("./TaskModal").then((module) => ({ default: module.TaskModal })),
);
const OpenCodes = lazy(() => import("./OpenCodes"));
const Configuration = lazy(() => import("./Configuration"));

function preference(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}
function savePreference(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* A private browser may disable storage. */
  }
}
function SearchBox({
  value,
  change,
}: {
  value: string;
  change: (q: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  useEffect(() => {
    if (draft === value) return;
    const timer = setTimeout(() => change(draft), 300);
    return () => clearTimeout(timer);
  }, [draft, value, change]);
  return (
    <label className="search-box">
      <Search size={16} />
      <input
        id="task-search"
        aria-label="Search tasks"
        placeholder="Search tasks…"
        value={draft}
        maxLength={300}
        onChange={(event) => setDraft(event.target.value)}
      />
      {draft ? (
        <button
          className="icon-button"
          aria-label="Clear search"
          onClick={() => {
            setDraft("");
            change("");
          }}
        >
          <X size={13} />
        </button>
      ) : (
        <kbd>/</kbd>
      )}
    </label>
  );
}
function AgentChip({
  agent,
  filter,
  open,
}: {
  agent: Agent;
  filter: () => void;
  open: (id: number) => void;
}) {
  const first = agent.assignments[0];
  const now = useNow();
  const unseen = agent.last_seen ? now - Date.parse(agent.last_seen) : Infinity;
  const overdue =
    first?.status === "in_progress" &&
    (unseen > 900000 ||
      (first.heartbeat_at && now - Date.parse(first.heartbeat_at) > 900000));
  let label = unseen > 300000 ? "Away" : "Idle";
  if (first)
    label =
      first.status === "blocked"
        ? "Blocked"
        : first.status === "review"
          ? "In review"
          : overdue
            ? "Overdue"
            : unseen > 300000
              ? "Quiet"
              : "Working";
  if (!agent.last_seen) label = first ? "Awaiting agent" : "Not seen";
  if (!agent.is_active) label = "Inactive";
  const attention = ["Blocked", "Overdue", "Quiet", "Awaiting agent"].includes(
    label,
  );
  return (
    <div
      className={`agent-chip ${attention ? "attention" : first ? "working" : ""}`}
    >
      <button
        className="agent-filter"
        onClick={filter}
        aria-label={`Filter by ${agent.name}`}
      >
        <Avatar name={agent.name} small />
        <strong>{agent.name}</strong>
        <span
          className={`presence-dot ${!agent.is_active ? "inactive" : attention ? "blocked" : first ? "working" : "idle"}`}
        />
      </button>
      {first ? (
        <button
          className="agent-assignment"
          title={first.title}
          onClick={() => open(first.id)}
        >
          {label}
          <span>#{first.id}</span>
          <span className="agent-seen">
            · <Age value={agent.last_seen} />
          </span>
        </button>
      ) : (
        <span className="agent-assignment">
          {label} · <Age value={agent.last_seen} />
        </span>
      )}
    </div>
  );
}
export default function App() {
  const { location, navigate } = useLocationState();
  const { connection, highlighted } = useLiveUpdates();
  const [theme, setTheme] = useState(() =>
    preference("agent-ops-theme", "dark") === "light" ? "light" : "dark",
  );
  const [density, setDensity] = useState(() =>
    preference("agent-ops-density", "comfortable") === "compact"
      ? "compact"
      : "comfortable",
  );
  const [activityOpen, setActivityOpen] = useState(false);
  const [agentsOpen, setAgentsOpen] = useState(true);
  const [trail, setTrail] = useState<string[]>([]);
  const trigger = useRef<HTMLElement | null>(null);
  const root = document.getElementById("root")!;
  const { filters } = location;
  const workspace = useQuery({
    queryKey: ["workspace", filters],
    queryFn: () => get<Workspace>(`/dashboard/workspace/?${query(filters)}`),
  });
  const data = workspace.data;
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    savePreference("agent-ops-theme", theme);
  }, [theme]);
  useEffect(() => {
    savePreference("agent-ops-density", density);
  }, [density]);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if (
        event.key === "/" &&
        !location.task &&
        !(
          event.target instanceof HTMLElement &&
          event.target.closest("input, textarea, select, [contenteditable]")
        )
      ) {
        event.preventDefault();
        document.getElementById("task-search")?.focus();
      }
    };
    document.addEventListener("keydown", shortcut);
    return () => document.removeEventListener("keydown", shortcut);
  }, [location.task]);
  const change = useCallback(
    (changes: Record<string, string | number | null>) => navigate(changes),
    [navigate],
  );
  const filter = useCallback(
    (changes: Record<string, string | number | null>) =>
      navigate({ section: null, ...changes, page: null }),
    [navigate],
  );
  const search = useCallback((q: string) => filter({ q }), [filter]);
  function openTask(id: number, element?: HTMLElement) {
    if (location.task) {
      setTrail((previous) => [...previous, location.task]);
      navigate({ task: id });
    } else {
      trigger.current = element || (document.activeElement as HTMLElement);
      setTrail([]);
      navigate({ task: id }, true, true);
    }
  }
  // Focus restoration lives in the modal's own close callback; this only clears
  // navigation state once the dialog has finished closing.
  function closeTask() {
    setTrail([]);
    if (window.history.state?.taskModalEntry) window.history.back();
    else navigate({ task: null });
  }
  function previousTask() {
    const previous = trail.at(-1);
    if (previous) {
      navigate({ task: previous });
      setTrail((value) => value.slice(0, -1));
    }
  }
  const project = data?.projects.find((item) => item.slug === filters.project);
  const activeFilters = Object.values(filters).filter(Boolean).length;
  const reset = () =>
    filter({
      project: null,
      q: null,
      claimed_by: null,
      status: null,
      ready: null,
      ordering: "priority",
    });
  const expired =
    connection === "expired" ||
    (workspace.error instanceof ApiError && workspace.error.status === 401);
  const connectionLabel = expired
    ? "Session expired"
    : workspace.isError
      ? "Sync interrupted"
      : connection === "live"
        ? "Live · WebSocket"
        : connection === "connecting"
          ? "Connecting"
          : "Reconnecting";
  return (
    <ClockProvider serverTime={data?.server_time}>
      <div className={`app-shell density-${density}`}>
        <aside className="sidebar">
          <a
            className="brand"
            href="/dashboard/"
            aria-label="Agent Ops workspace"
          >
            <span className="brand-mark">
              <Command size={20} />
            </span>
            <span>
              agent<span className="brand-light">ops</span>
              <span className="brand-period">.</span>
            </span>
          </a>
          <div className="workspace-label">
            <span className="workspace-emblem">A</span>
            <div>
              <strong>Your workspace</strong>
              <span>Agent Task Coordinator</span>
            </div>
            <ChevronDown size={13} />
          </div>
          <div className="nav-caption">WORKSPACE</div>
          <button
            className={`nav-link ${location.section === "tasks" ? "active" : ""}`}
            onClick={reset}
          >
            <LayoutGrid size={17} />
            All tasks<span className="nav-count">{data?.total ?? "—"}</span>
          </button>
          <button
            className={`nav-link ${location.section === "opencodes" ? "active" : ""}`}
            onClick={() => navigate({ section: "opencodes", task: null }, true)}
          >
            <Monitor size={17} />
            OpenCodes
          </button>
          <button
            className={`nav-link ${agentsOpen ? "subtle-active" : ""}`}
            onClick={() => {
              if (location.section !== "tasks") {
                navigate({ section: null });
                setAgentsOpen(true);
              } else setAgentsOpen(!agentsOpen);
            }}
          >
            <Users size={17} />
            Agents
            <span className="nav-count">{data?.agents.length ?? "—"}</span>
          </button>
          <button
            className={`nav-link ${activityOpen ? "subtle-active" : ""}`}
            onClick={() => {
              if (location.section !== "tasks") {
                navigate({ section: null });
                setActivityOpen(true);
              } else setActivityOpen(!activityOpen);
            }}
          >
            <Activity size={17} />
            Activity{connection === "live" && <span className="tiny-dot" />}
          </button>
          <div className="nav-caption project-caption">
            PROJECTS<span>{data?.projects.length || 0}</span>
          </div>
          <div className="project-navigation">
            {data?.projects.map((item) => (
              <button
                key={item.slug}
                className={`project-nav ${filters.project === item.slug ? "selected" : ""}`}
                onClick={() =>
                  filter({
                    project: filters.project === item.slug ? null : item.slug,
                  })
                }
              >
                <span className={`project-square ${tone(item.slug)}`} />
                {item.name}
                <span>{item.active_count}</span>
              </button>
            ))}
          </div>
          <div className="sidebar-bottom">
            <button
              className={`nav-link ${location.section === "configuration" ? "active" : ""}`}
              onClick={() =>
                navigate({ section: "configuration", task: null }, true)
              }
            >
              <Settings size={16} />
              Configuration
            </button>
            <a
              className="nav-link"
              href="https://github.com/aaronx3011/agents-task-manager/issues/3"
              target="_blank"
              rel="noopener noreferrer"
            >
              <BookOpen size={16} />
              Documentation
              <ArrowUpRight size={13} />
            </a>
            <div className="user-bar">
              <Avatar name={root.dataset.username || "Staff"} />
              <div>
                <strong>{root.dataset.username || "Staff"}</strong>
                <span>Staff workspace</span>
              </div>
              <a
                className="icon-button"
                href="/admin/"
                aria-label="Open Django admin"
              >
                <ArrowUpRight size={15} />
              </a>
            </div>
          </div>
        </aside>
        <main className="main-workspace">
          <header className="topbar">
            <div className="breadcrumbs">
              <Layers3 size={15} />
              <span>Workspace</span>
              <ChevronRight size={13} />
              <strong>
                {location.section === "opencodes"
                  ? "Connected OpenCodes"
                  : location.section === "configuration"
                    ? "Configuration"
                    : project?.name || "All projects"}
              </strong>
            </div>
            <div className="topbar-actions">
              <span
                className={`connection-badge ${connection === "live" && !workspace.isError ? "connected" : ""}`}
                role="status"
              >
                <span className="live-dot" />
                {connectionLabel}
              </span>
              <button
                className="icon-button theme-toggle"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
              >
                {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
              </button>
            </div>
          </header>
          <div
            className="mobile-workspace-nav view-tabs"
            role="group"
            aria-label="Workspace section"
          >
            <button
              aria-label="All tasks"
              aria-pressed={location.section === "tasks"}
              onClick={reset}
            >
              Tasks
            </button>
            <button
              aria-pressed={location.section === "opencodes"}
              onClick={() =>
                navigate({ section: "opencodes", task: null }, true)
              }
            >
              OpenCodes
            </button>
            <button
              aria-pressed={location.section === "configuration"}
              onClick={() =>
                navigate({ section: "configuration", task: null }, true)
              }
            >
              Configuration
            </button>
          </div>
          {location.section === "opencodes" ? (
            <Suspense
              key="opencodes"
              fallback={<Loading label="Opening OpenCodes…" />}
            >
              <OpenCodes
                worker={location.worker}
                session={location.session}
                navigate={navigate}
                openTask={openTask}
              />
            </Suspense>
          ) : location.section === "configuration" ? (
            <Suspense
              key="configuration"
              fallback={<Loading label="Opening configuration…" />}
            >
              <Configuration />
            </Suspense>
          ) : (
            <>
              <section className="workspace-heading">
                <div>
                  <div className="eyebrow">YOUR AGENTS, IN SYNC</div>
                  <h1 id="workspace-title" tabIndex={-1}>
                    {project?.name || "Workspace overview"}
                    <span className="heading-dot">.</span>
                  </h1>
                  <p>A clear picture of the work. As it happens.</p>
                </div>
                <div className="heading-right">
                  {root.dataset.demo === "true" && (
                    <span className="demo-label">
                      <Radio size={13} />
                      LIVE MCP DEMO
                    </span>
                  )}
                  <span className="read-only">
                    <LockIcon />
                    Observation mode
                  </span>
                </div>
              </section>
              <div className="workspace-toolbar">
                <div className="view-tabs" role="group" aria-label="Task view">
                  <button
                    aria-pressed={location.view === "kanban"}
                    disabled={filters.status === "cancelled"}
                    onClick={() => change({ view: "kanban" })}
                  >
                    <LayoutGrid size={15} />
                    Kanban
                  </button>
                  <button
                    aria-pressed={location.view === "list"}
                    onClick={() => change({ view: "list" })}
                  >
                    <List size={16} />
                    List
                  </button>
                </div>
                <div className="toolbar-spacer" />
                <SearchBox value={filters.q} change={search} />
                <button
                  className={`icon-button density-button ${density === "compact" ? "selected" : ""}`}
                  aria-label={`Use ${density === "compact" ? "comfortable" : "compact"} density`}
                  onClick={() =>
                    setDensity(
                      density === "compact" ? "comfortable" : "compact",
                    )
                  }
                >
                  <ArrowDownWideNarrow size={17} />
                </button>
                <button
                  className={`icon-button activity-toggle ${activityOpen ? "selected" : ""}`}
                  aria-label={
                    activityOpen ? "Hide activity panel" : "Show activity panel"
                  }
                  onClick={() => setActivityOpen(!activityOpen)}
                >
                  {activityOpen ? (
                    <PanelRightClose size={17} />
                  ) : (
                    <PanelRightOpen size={17} />
                  )}
                </button>
              </div>
              <div className="filters-bar">
                <span className="filter-label">
                  <SlidersHorizontal size={13} />
                  Filters
                </span>
                <select
                  aria-label="Project filter"
                  value={filters.project}
                  onChange={(event) => filter({ project: event.target.value })}
                >
                  <option value="">All projects</option>
                  {data?.projects.map((item) => (
                    <option key={item.slug} value={item.slug}>
                      {item.name}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="Agent filter"
                  value={filters.claimed_by}
                  onChange={(event) =>
                    filter({ claimed_by: event.target.value })
                  }
                >
                  <option value="">All agents</option>
                  <option value="__unclaimed__">Unassigned</option>
                  {data?.agents.map((agent) => (
                    <option key={agent.name} value={agent.name}>
                      {agent.name}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="Status filter"
                  value={filters.status}
                  onChange={(event) =>
                    filter({
                      status: event.target.value,
                      ...(event.target.value === "cancelled"
                        ? { view: "list" }
                        : {}),
                    })
                  }
                >
                  <option value="">All statuses</option>
                  {STATUSES.map((status) => (
                    <option key={status} value={status}>
                      {LABELS[status]}
                    </option>
                  ))}
                </select>
                <button
                  className={`filter-chip ${filters.ready ? "enabled" : ""}`}
                  aria-pressed={Boolean(filters.ready)}
                  onClick={() =>
                    filter({ ready: filters.ready ? null : "true" })
                  }
                >
                  <Check size={12} />
                  Ready only
                </button>
                {activeFilters > 0 && (
                  <button className="clear-filters" onClick={reset}>
                    <X size={12} />
                    Clear {activeFilters}
                  </button>
                )}
                <span className="result-count">
                  {data?.total ?? "—"} matching tasks
                </span>
              </div>
              {agentsOpen && (
                <div className="team-strip">
                  <span className="team-label">
                    <Users size={13} />
                    The team
                  </span>
                  <div className="team-agents">
                    {data?.agents.map((agent) => (
                      <AgentChip
                        key={agent.name}
                        agent={agent}
                        filter={() =>
                          filter({
                            claimed_by:
                              filters.claimed_by === agent.name
                                ? null
                                : agent.name,
                          })
                        }
                        open={openTask}
                      />
                    ))}
                  </div>
                  <button
                    className="icon-button"
                    aria-label="Hide agent strip"
                    onClick={() => setAgentsOpen(false)}
                  >
                    <X size={14} />
                  </button>
                </div>
              )}
              <div className="content-summary">
                <span>
                  {location.view === "kanban" ? "Task board" : "All tasks"}
                  <span className="count-bubble">{data?.total ?? "—"}</span>
                </span>
                <div>
                  <span className="summary-ready">
                    <span className="tiny-dot" />
                    {data?.ready_count || 0} ready
                  </span>
                  <span className="summary-note">
                    {location.view === "kanban"
                      ? "Sorted by priority"
                      : "Live task list"}
                  </span>
                  {location.view === "kanban" &&
                    Boolean(data?.counts.cancelled) && (
                      <button
                        className="summary-note"
                        onClick={() =>
                          filter({ view: "list", status: "cancelled" })
                        }
                      >
                        {data?.counts.cancelled} cancelled · view list
                      </button>
                    )}
                </div>
              </div>
              {expired && (
                <div className="session-notice">
                  Your session has ended.{" "}
                  <a
                    href={`/admin/login/?next=${encodeURIComponent(window.location.pathname + window.location.search)}`}
                  >
                    Sign in again
                  </a>
                </div>
              )}
              <div className="workspace-content">
                <div className="primary-content">
                  {workspace.isLoading ? (
                    <Loading />
                  ) : workspace.error ? (
                    <ErrorState
                      error={workspace.error}
                      retry={() => void workspace.refetch()}
                    />
                  ) : data?.total === 0 ? (
                    <Empty
                      title={
                        activeFilters
                          ? "No tasks match your filters"
                          : "Your workspace is ready"
                      }
                      description={
                        activeFilters
                          ? "Try a different search or clear your filters."
                          : "Tasks will appear here when your agents create work."
                      }
                    >
                      {activeFilters > 0 && (
                        <button className="button secondary" onClick={reset}>
                          Clear filters
                        </button>
                      )}
                    </Empty>
                  ) : location.view === "kanban" ? (
                    <Board
                      filters={filters}
                      open={openTask}
                      highlighted={highlighted}
                    />
                  ) : (
                    <TaskList
                      filters={filters}
                      ordering={location.ordering}
                      page={location.page}
                      change={change}
                      open={openTask}
                    />
                  )}
                </div>
                {activityOpen && (
                  <aside className="activity-panel" aria-label="Live activity">
                    <header>
                      <Activity size={15} />
                      <h2>Activity</h2>
                      <span className="live-dot" />
                    </header>
                    <p className="panel-description">
                      The latest updates from your agents.
                    </p>
                    <div className="activity-events">
                      {data?.events.map((event) => (
                        <button
                          className="activity-item"
                          key={event.id}
                          onClick={() => openTask(event.task_id)}
                        >
                          <span
                            className={`activity-symbol event-${event.event_type}`}
                          >
                            <Activity size={13} />
                          </span>
                          <div>
                            <strong>
                              {event.agent || "System / human"}
                              <span>
                                {event.event_type.replaceAll("_", " ")}
                              </span>
                            </strong>
                            <p>{event.title}</p>
                            {event.message && <small>{event.message}</small>}
                            <Age value={event.created_at} />
                          </div>
                        </button>
                      ))}
                      {data && !data.events.length && (
                        <Empty title="No activity in this view" />
                      )}
                    </div>
                  </aside>
                )}
              </div>
              <footer className="workspace-footer">
                <span>
                  <span className="tiny-dot" />
                  {workspace.isFetching
                    ? "Syncing committed state…"
                    : "All changes synced"}
                </span>
                <span>
                  <CircleHelp size={12} />
                  Click a task to explore its details
                </span>
              </footer>
            </>
          )}
        </main>
        {location.task && (
          <ModalBoundary close={closeTask}>
            <Suspense
              fallback={
                <div className="modal-overlay">
                  <Loading label="Opening task…" />
                </div>
              }
            >
              <TaskModal
                rawId={location.task}
                close={closeTask}
                open={openTask}
                back={previousTask}
                hasBack={trail.length > 0}
                trigger={trigger}
              />
            </Suspense>
          </ModalBoundary>
        )}
      </div>
    </ClockProvider>
  );
}
function LockIcon() {
  return (
    <span className="observation-icon" aria-hidden="true">
      ◉
    </span>
  );
}
