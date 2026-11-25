import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  Pause,
  Play,
  Plus,
  Settings,
  Square,
  X,
} from "lucide-react";
import { get, post } from "./api";
import type { LocationState, Page, Project, Status } from "./types";
import { LABELS, STATUSES } from "./types";
import { refreshTaskViews } from "./task-sync";
import type { ModelChoice, Worker } from "./opencode-types";
import { ErrorState, Loading } from "./ui";
import { useAccess } from "./auth";
import {
  CARD_HANDOFF_CHOICES,
  cardHandoffLabel,
  effectiveHandoffMode,
  HANDOFF_MODES,
  handoffCopy,
  isCardHandoff,
  normalizeHandoffMode,
  type CardHandoff,
  type HandoffMode,
} from "./handoff";
import "./auth.css";
import "./planning.css";

interface SettingsData {
  worker: string;
  directory: string;
  model: ModelChoice | null;
  agent: string;
  context: string;
  instructions: string;
  /** Hand-off default for cards launched from this project (#137). */
  handoff_mode: HandoffMode;
  templates: { name: string; text: string }[];
  columns: string[];
}
interface Card {
  id?: number;
  title: string;
  description: string;
  column: string;
  position?: number;
  labels: string[];
  checklist: { text: string; done: boolean }[];
  instructions: string;
  /** Blank runs on the project's default worker. */
  worker?: string;
  agent: string;
  model: ModelChoice | null;
  /** Blank inherits the project default; the server also resolves it for us. */
  handoff_mode?: CardHandoff;
  effective_handoff_mode?: HandoffMode;
  task?: number | null;
  task_status?: Status | null;
}
interface Job {
  id: number;
  card: number;
  title: string;
  status: string;
  position: number;
  worker: string;
  agent: string;
  model: ModelChoice | null;
  session: string;
  task: number | null;
  task_status: Status | null;
  detail: string;
  prompt: string;
  snapshot: Record<string, unknown>;
  pipe: number | null;
  stage: { id: number; position: number; name: string; role: string } | null;
}
interface PipeStageView {
  id: number;
  position: number;
  name: string;
  role: string;
  status: string;
  instructions: string;
  worker: string;
  agent: string;
  model: ModelChoice | null;
  directory: string;
  requires_approval: boolean;
  artifact: string;
  attempts: number;
  card: number | null;
  task: number | null;
  job: number | null;
  job_status: string | null;
  job_detail: string;
  session: string;
  task_status: Status | null;
}
interface PipeView {
  id: number;
  title: string;
  description: string;
  status: string;
  detail: string;
  on_failure: "stop" | "loop";
  max_retries: number;
  retries_used: number;
  approved_stage: number;
  stages: PipeStageView[];
}
interface Plan {
  project: string;
  name: string;
  settings: SettingsData;
  revision: number;
  cards: Card[];
  jobs: Job[];
  playing: boolean;
  pause_reason: string;
  pipes: PipeView[];
}
type Navigate = (
  changes: Record<string, string | number | null>,
  push?: boolean,
) => void;

/** Marks cards that will launch without stopping for approve/deny prompts (#137).
 *
 * Only semi_auto is badged: "off" is the default and badging every card would
 * say nothing. The server resolves inheritance, so trust it when present.
 */
function HandoffBadge({
  card,
  projectMode,
}: {
  card: Card;
  projectMode: HandoffMode;
}) {
  const mode = card.effective_handoff_mode
    ? normalizeHandoffMode(card.effective_handoff_mode)
    : effectiveHandoffMode(card.handoff_mode, projectMode);
  if (mode !== "semi_auto") return null;
  return (
    <span className="planning-handoff-badge" title={handoffCopy(mode).summary}>
      Semi-auto approvals
    </span>
  );
}

export default function Planning({
  projects,
  project,
  navigate,
  openTask,
  newProject,
  tab,
}: {
  projects: Project[];
  project: string;
  navigate: Navigate;
  openTask: (id: number) => void;
  newProject: boolean;
  tab: LocationState["planningTab"];
}) {
  const { canWrite } = useAccess();
  const slug = project || projects[0]?.slug || "";
  return (
    <section className="planning-page">
      <header className="planning-heading">
        <div>
          <div className="eyebrow">PLAN → EXECUTE → REVIEW</div>
          <h1>
            {projects.find((p) => p.slug === slug)?.name || "Project planning"}
          </h1>
          <p>Organize your work. Launch contextual sessions in order.</p>
        </div>
        <label>
          Project
          <select
            aria-label="Planning project"
            value={slug}
            onChange={(e) =>
              navigate({ project: e.target.value, planning_tab: null }, true)
            }
          >
            {projects.map((p) => (
              <option key={p.slug} value={p.slug}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        {canWrite && (
          <button onClick={() => navigate({ new_project: "true" })}>
            <Plus size={16} /> New project
          </button>
        )}
      </header>
      {slug ? (
        <ProjectPlanning
          key={slug}
          slug={slug}
          navigate={navigate}
          openTask={openTask}
          tab={tab}
        />
      ) : (
        <p>Create your first project with New project to start planning.</p>
      )}
      {newProject && canWrite && (
        <NewProjectForm
          close={() => navigate({ new_project: null })}
          created={(slug) =>
            navigate(
              {
                section: "planning",
                project: slug,
                new_project: null,
                planning_tab: "settings",
                q: null,
                claimed_by: null,
                status: null,
                ready: null,
                task: null,
                page: null,
              },
              true,
            )
          }
        />
      )}
    </section>
  );
}

function NewProjectForm({
  close,
  created,
}: {
  close: () => void;
  created: (slug: string) => void;
}) {
  const client = useQueryClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [kind, setKind] = useState("code");
  const [repo, setRepo] = useState("");
  const [docs, setDocs] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => previous?.focus();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="planning-dialog planning-project-dialog"
      aria-labelledby="new-project-title"
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) close();
      }}
    >
      <header>
        <h2 id="new-project-title">New project</h2>
        <button disabled={busy} aria-label="Close new project" onClick={close}>
          <X size={18} />
        </button>
      </header>
      <p>
        Create a project, then configure its worker, directory, model and
        prompts.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (busy) return;
          setBusy(true);
          setError("");
          void post<Project>("/api/projects", {
            name,
            slug,
            kind,
            repo_url: repo,
            docs_path: docs,
          })
            .then(async (project) => {
              await client.invalidateQueries({ queryKey: ["workspace"] });
              created(project.slug);
            })
            .catch((e) =>
              setError(
                e instanceof Error ? e.message : "Project creation failed",
              ),
            )
            .finally(() => setBusy(false));
        }}
      >
        <fieldset disabled={busy}>
          <label>
            Project name
            <input
              autoFocus
              required
              maxLength={200}
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (!slugEdited)
                  setSlug(
                    e.target.value
                      .normalize("NFKD")
                      .replace(/[\u0300-\u036f]/g, "")
                      .toLowerCase()
                      .replace(/[^a-z0-9]+/g, "-")
                      .replace(/^-+|-+$/g, "")
                      .slice(0, 50),
                  );
              }}
            />
          </label>
          <label>
            Project slug
            <input
              required
              maxLength={50}
              pattern="[a-zA-Z0-9_-]+"
              value={slug}
              onChange={(e) => {
                setSlug(e.target.value);
                setSlugEdited(true);
              }}
              aria-describedby="project-slug-help"
            />
          </label>
          <small id="project-slug-help">
            Unique identifier for links and agent tools. Letters, numbers,
            hyphens and underscores.
          </small>
          <label>
            Project kind
            <select
              aria-label="Project kind"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              <option value="code">Code</option>
              <option value="design">Design</option>
              <option value="content">Content</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label>
            Repository URL (optional)
            <input
              type="url"
              maxLength={200}
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="https://github.com/you/project"
            />
          </label>
          <label>
            Documentation reference (optional)
            <input
              maxLength={500}
              value={docs}
              onChange={(e) => setDocs(e.target.value)}
              placeholder="Documentation URL or path"
            />
          </label>
        </fieldset>
        {error && (
          <p className="planning-error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <button type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create project"}
          </button>
          <button type="button" disabled={busy} onClick={close}>
            Cancel
          </button>
        </footer>
      </form>
    </dialog>
  );
}

function ProjectPlanning({
  slug,
  navigate,
  openTask,
  tab,
}: {
  slug: string;
  navigate: Navigate;
  openTask: (id: number) => void;
  tab: LocationState["planningTab"];
}) {
  const { canWrite } = useAccess();
  const client = useQueryClient();
  const setTab = (tab: LocationState["planningTab"]) =>
    navigate({ planning_tab: tab === "overview" ? null : tab }, true);
  const [editor, setEditor] = useState<Card | null>(null);
  const [editorRevision, setEditorRevision] = useState(0);
  const [launch, setLaunch] = useState<Card | null>(null);
  const [instructions, setInstructions] = useState("");
  const [template, setTemplate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dragged, setDragged] = useState<number | null>(null);
  const dialogOpen = !!editor || !!launch;
  useEffect(() => {
    if (!dialogOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.querySelector<HTMLElement>(".planning-dialog");
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, [tabindex="0"]',
        ) || [],
      );
    focusable()
      .find((el) => el.tagName === "INPUT" || el.tagName === "TEXTAREA")
      ?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setEditor(null);
        setLaunch(null);
      }
      if (event.key === "Tab") {
        const controls = focusable();
        const first = controls[0],
          last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      previous?.focus();
    };
  }, [dialogOpen]);
  const plan = useQuery({
    queryKey: ["planning", slug],
    queryFn: ({ signal }) =>
      get<Plan>(`/api/projects/${slug}/planning`, signal),
  });
  const workers = useQuery({
    queryKey: ["planning-workers"],
    queryFn: () => get<Page<Worker>>("/api/workers?limit=500"),
    refetchInterval: 10000,
  });
  const data = plan.data;
  async function mutate(
    values: Record<string, unknown>,
    revision = data?.revision,
  ) {
    if (!canWrite) {
      setError("Viewers have read-only access to planning.");
      return false;
    }
    if (busy || revision === undefined) return false;
    setBusy(true);
    setError("");
    try {
      const result = await post<Plan>(`/api/projects/${slug}/planning`, {
        revision,
        ...values,
      });
      client.setQueryData(["planning", slug], result);
      void refreshTaskViews(client);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save changes");
      void plan.refetch();
      return false;
    } finally {
      setBusy(false);
    }
  }
  if (!data)
    return plan.isError ? (
      <ErrorState error={plan.error} retry={() => void plan.refetch()} />
    ) : (
      <Loading label="Loading project planning…" />
    );
  const pipes = data.pipes ?? [];
  const queued = data.jobs.filter((j) => j.status === "queued");
  const active = data.jobs.find((j) => j.status === "running");
  const workerList = workers.data?.items || [];
  // A card's own worker overrides the project default; agent/model choices
  // and their inherited defaults follow whichever worker the card runs on.
  const editorWorkerName = editor?.worker || data.settings.worker;
  const editorInherits = editorWorkerName === data.settings.worker;
  const editorWorker = workerList.find((w) => w.name === editorWorkerName);
  const workerAgents = editorWorker?.agents || [];
  const workerModels = editorWorker?.models || [];
  const launchWorker = launch?.worker || data.settings.worker;
  const launchInherits = launchWorker === data.settings.worker;
  const chooseLaunchWorker = (name: string) => {
    if (!launch) return;
    const next = { ...launch, worker: name, agent: "", model: null };
    void mutate({ operation: "card", card: editableCard(next) }).then(
      (ok) => ok && setLaunch(next),
    );
  };
  const edit = (card: Card) => {
    setEditor({ ...card });
    setEditorRevision(data.revision);
  };
  const move = (card: Card, column: string, position = card.position) =>
    void mutate({
      operation: "card",
      card: { ...editableCard(card), column, position },
    });
  const session = (job: Job) =>
    navigate(
      {
        section: "sessions",
        worker: job.worker,
        session: job.session,
        task: null,
      },
      true,
    );
  return (
    <>
      <div className="planning-toolbar">
        <div
          className="view-tabs"
          role="group"
          aria-label="Project planning view"
        >
          {(["overview", "board", "queue", "pipes", "settings"] as const).map(
            (t) => (
              <button
                key={t}
                aria-pressed={tab === t}
                onClick={() => setTab(t)}
              >
                {t === "settings" && <Settings size={14} />}{" "}
                {t === "overview"
                  ? "Planning"
                  : t === "queue"
                    ? `Queue (${queued.length})`
                    : t === "pipes"
                      ? `Pipes (${pipes.length})`
                      : t[0].toUpperCase() + t.slice(1)}
              </button>
            ),
          )}
        </div>
        <span className="planning-status" role="status">
          {data.playing
            ? active
              ? `Running: ${active.title}`
              : "Waiting for worker sync"
            : data.pause_reason || "Queue paused"}
        </span>
        <fieldset className="role-fieldset" disabled={!canWrite}>
          <button
            disabled={busy || (!active && !queued.length)}
            onClick={() =>
              void mutate({ operation: data.playing ? "pause" : "play" })
            }
          >
            {data.playing ? <Pause size={15} /> : <Play size={15} />}{" "}
            {data.playing ? "Pause" : "Play"}
          </button>
          {active && (
            <button
              disabled={busy}
              onClick={() => void mutate({ operation: "stop" })}
            >
              <Square size={14} /> Stop current
            </button>
          )}
        </fieldset>
      </div>
      {!canWrite && (
        <p className="auth-notice" role="note">
          Viewers can follow planning but not change it. Ask an owner for member
          access.
        </p>
      )}
      {error && (
        <div className="planning-error" role="alert">
          {error}
        </div>
      )}
      {plan.isError && (
        <div className="planning-error" role="alert">
          Updates interrupted: {plan.error.message}
        </div>
      )}
      <fieldset className="role-fieldset" disabled={!canWrite}>
        {tab === "overview" ? (
          <section
            className="planning-overview"
            aria-label="Project planning overview"
          >
            <div className="planning-overview-summary">
              <article>
                <h2>Planning</h2>
                <p>
                  Shape the next steps for {data.name}. Select a work item to
                  refine its scope and acceptance criteria.
                </p>
                <button
                  onClick={() =>
                    edit({
                      title: "",
                      description: "",
                      column: data.settings.columns[0],
                      labels: [],
                      checklist: [],
                      instructions: "",
                      agent: "",
                      model: null,
                    })
                  }
                >
                  <Plus size={16} /> New work item
                </button>
              </article>
              <article>
                <h2>Execution</h2>
                <p>
                  {data.cards.length} work items · {queued.length} queued ·{" "}
                  {data.jobs.filter((job) => job.status === "completed").length}{" "}
                  completed runs
                </p>
                {active && <p>Current: {active.title}</p>}
                <button onClick={() => setTab("queue")}>
                  View execution queue
                </button>
              </article>
            </div>
            {data.settings.context && (
              <article className="planning-overview-context">
                <h2>Project context</h2>
                <p>{data.settings.context}</p>
              </article>
            )}
            <h2>Planned work</h2>
            {!data.cards.length && (
              <p>
                No work items yet. Add your first work item to start planning.
              </p>
            )}
            <div className="planning-work-items">
              {data.cards.map((card) => (
                <article className="planning-card" key={card.id}>
                  <small>{card.column}</small>
                  <button
                    className="planning-card-title"
                    onClick={() => edit(card)}
                  >
                    {card.title}
                  </button>
                  {card.description && <p>{card.description.slice(0, 240)}</p>}
                  <div className="planning-labels">
                    {card.labels.map((label) => (
                      <span key={label}>{label}</span>
                    ))}
                  </div>
                  {!!card.checklist.length && (
                    <small>
                      Checklist{" "}
                      {card.checklist.filter((item) => item.done).length}/
                      {card.checklist.length}
                    </small>
                  )}
                  <HandoffBadge
                    card={card}
                    projectMode={data.settings.handoff_mode}
                  />
                  {card.task && (
                    <button
                      className="planning-task-link"
                      onClick={() => openTask(card.task!)}
                    >
                      Task #{card.task} · {card.task_status}
                    </button>
                  )}
                  {card.task && card.task_status && (
                    <TaskStatusEditor
                      task={card.task}
                      status={card.task_status}
                    />
                  )}
                  <footer>
                    <button
                      disabled={
                        busy ||
                        data.jobs.some(
                          (job) =>
                            job.card === card.id &&
                            ["queued", "running"].includes(job.status),
                        )
                      }
                      onClick={() => {
                        setLaunch(card);
                        setInstructions(card.instructions);
                        setTemplate("");
                      }}
                    >
                      Run / Queue
                    </button>
                  </footer>
                </article>
              ))}
            </div>
          </section>
        ) : tab === "settings" ? (
          <SettingsForm
            initial={data.settings}
            revision={data.revision}
            workers={workers.data?.items || []}
            busy={busy}
            save={(settings, revision) =>
              mutate({ operation: "settings", settings }, revision)
            }
          />
        ) : tab === "board" ? (
          <>
            <div className="planning-board">
              {data.settings.columns.map((column) => (
                <section
                  className="planning-column"
                  key={column}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => {
                    e.preventDefault();
                    const card = data.cards.find((c) => c.id === dragged);
                    if (card)
                      move(
                        card,
                        column,
                        Math.max(
                          0,
                          ...data.cards
                            .filter((c) => c.column === column)
                            .map((c) => c.position || 0),
                        ) + 1,
                      );
                    setDragged(null);
                  }}
                >
                  <header>
                    <h2>
                      {column}{" "}
                      <span>
                        {data.cards.filter((c) => c.column === column).length}
                      </span>
                    </h2>
                    <button
                      aria-label={`Add card to ${column}`}
                      onClick={() =>
                        edit({
                          title: "",
                          description: "",
                          column,
                          labels: [],
                          checklist: [],
                          instructions: "",
                          agent: "",
                          model: null,
                        })
                      }
                    >
                      <Plus size={16} />
                    </button>
                  </header>
                  {data.cards
                    .filter((c) => c.column === column)
                    .map((card) => (
                      <article
                        className="planning-card"
                        key={card.id}
                        draggable={!busy}
                        onDragStart={() => setDragged(card.id!)}
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={(e) => {
                          e.stopPropagation();
                          e.preventDefault();
                          const source = data.cards.find(
                            (c) => c.id === dragged,
                          );
                          if (source && source.id !== card.id)
                            move(source, column, card.position || 0);
                          setDragged(null);
                        }}
                      >
                        <div className="planning-labels">
                          {card.labels.map((label) => (
                            <span key={label}>{label}</span>
                          ))}
                        </div>
                        <button
                          className="planning-card-title"
                          onClick={() => edit(card)}
                        >
                          {card.title}
                        </button>
                        {card.description && (
                          <p>{card.description.slice(0, 160)}</p>
                        )}
                        {!!card.checklist.length && (
                          <small>
                            Checklist{" "}
                            {card.checklist.filter((c) => c.done).length}/
                            {card.checklist.length}
                          </small>
                        )}
                        <HandoffBadge
                          card={card}
                          projectMode={data.settings.handoff_mode}
                        />
                        {card.task && (
                          <button
                            className="planning-task-link"
                            onClick={() => openTask(card.task!)}
                          >
                            Task #{card.task} · {card.task_status}
                          </button>
                        )}
                        {card.task && card.task_status && (
                          <TaskStatusEditor
                            task={card.task}
                            status={card.task_status}
                          />
                        )}
                        <footer>
                          <button
                            disabled={
                              busy ||
                              data.jobs.some(
                                (j) =>
                                  j.card === card.id &&
                                  ["queued", "running"].includes(j.status),
                              )
                            }
                            onClick={() => {
                              setLaunch(card);
                              setInstructions(card.instructions);
                              setTemplate("");
                            }}
                          >
                            Run / Queue
                          </button>
                          <select
                            aria-label={`Move ${card.title}`}
                            value={card.column}
                            onChange={(e) => move(card, e.target.value)}
                          >
                            {data.settings.columns.map((c) => (
                              <option key={c}>{c}</option>
                            ))}
                          </select>
                        </footer>
                      </article>
                    ))}
                </section>
              ))}
            </div>
            <p className="planning-help">
              Drag cards between columns, or use their Move menu. Board columns
              organize planning stages. Use Execution status on a linked task to
              update its status here and in All Tasks.
            </p>
          </>
        ) : tab === "pipes" ? (
          <PipesPanel
            pipes={pipes}
            workers={workerList}
            busy={busy}
            canWrite={canWrite}
            mutate={mutate}
            openTask={openTask}
            session={(stage) =>
              stage.session &&
              navigate(
                {
                  section: "sessions",
                  worker: stage.worker,
                  session: stage.session,
                  task: null,
                },
                true,
              )
            }
          />
        ) : (
          <div className="planning-queue">
            <p>
              One task at a time. Each gets a fresh session. Project context is
              frozen when a task is queued; editing a card (worker, agent,
              model, content) updates its queued task until it starts. Explicit
              task completion advances the queue; idle sessions do not.
            </p>
            {!data.jobs.length && (
              <p>
                Add cards to the queue from the board, arrange them, then press
                Play.
              </p>
            )}
            {data.jobs.map((job) => (
              <article className={`planning-job ${job.status}`} key={job.id}>
                <div>
                  <strong>{job.title}</strong>
                  <small>
                    {job.status} · {job.worker}
                    {job.agent ? ` · ${job.agent}` : ""}
                    {` · ${job.model?.modelID || "worker default model"}`}
                    {job.stage
                      ? ` · pipe stage ${job.stage.position} ${job.stage.name}`
                      : ""}
                    {job.task_status ? ` · task ${job.task_status}` : ""}
                  </small>
                  {job.detail && <p>{job.detail}</p>}
                </div>
                <div className="planning-job-actions">
                  {job.status === "queued" && (
                    <>
                      {[-1, 1].map((direction) => (
                        <button
                          key={direction}
                          aria-label={`Move ${job.title} ${direction < 0 ? "up" : "down"}`}
                          disabled={
                            busy ||
                            queued.indexOf(job) + direction < 0 ||
                            queued.indexOf(job) + direction >= queued.length
                          }
                          onClick={() => {
                            const ids = queued.map((j) => j.id);
                            const index = ids.indexOf(job.id);
                            [ids[index], ids[index + direction]] = [
                              ids[index + direction],
                              ids[index],
                            ];
                            void mutate({ operation: "reorder", job_ids: ids });
                          }}
                        >
                          {direction < 0 ? (
                            <ArrowUp size={15} />
                          ) : (
                            <ArrowDown size={15} />
                          )}
                        </button>
                      ))}
                      <button
                        disabled={busy}
                        onClick={() =>
                          void mutate({ operation: "remove", job_id: job.id })
                        }
                      >
                        Remove
                      </button>
                    </>
                  )}
                  {job.session && (
                    <button onClick={() => session(job)}>Open session</button>
                  )}
                  {job.task && (
                    <button onClick={() => openTask(job.task!)}>
                      Task #{job.task}
                    </button>
                  )}
                </div>
                <details>
                  <summary>Context and first prompt</summary>
                  <pre>
                    {job.prompt || JSON.stringify(job.snapshot, null, 2)}
                  </pre>
                </details>
                {job.task && ["running", "failed"].includes(job.status) && (
                  <TaskStatusEditor
                    task={job.task}
                    status={job.task_status || "in_progress"}
                  />
                )}
              </article>
            ))}
          </div>
        )}
        {editor && (
          <div className="planning-overlay">
            <section
              className="planning-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="card-title"
            >
              <header>
                <h2 id="card-title">{editor.id ? "Edit card" : "New card"}</h2>
                <button aria-label="Close card" onClick={() => setEditor(null)}>
                  <X size={18} />
                </button>
              </header>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void mutate(
                    { operation: "card", card: editableCard(editor) },
                    editorRevision,
                  ).then((ok) => {
                    if (ok) setEditor(null);
                  });
                }}
              >
                <label>
                  Title
                  <input
                    autoFocus
                    required
                    maxLength={300}
                    value={editor.title}
                    onChange={(e) =>
                      setEditor({ ...editor, title: e.target.value })
                    }
                  />
                </label>
                <label>
                  Description / acceptance criteria
                  <textarea
                    rows={6}
                    maxLength={12000}
                    value={editor.description}
                    onChange={(e) =>
                      setEditor({ ...editor, description: e.target.value })
                    }
                  />
                </label>
                <label>
                  Labels (comma separated)
                  <input
                    value={editor.labels.join(", ")}
                    onChange={(e) =>
                      setEditor({
                        ...editor,
                        labels: e.target.value.split(",").map((x) => x.trim()),
                      })
                    }
                  />
                </label>
                <fieldset>
                  <legend>Checklist</legend>
                  {editor.checklist.map((item, i) => (
                    <div className="planning-check" key={i}>
                      <input
                        aria-label={`Complete checklist item ${i + 1}`}
                        type="checkbox"
                        checked={item.done}
                        onChange={(e) =>
                          setEditor({
                            ...editor,
                            checklist: editor.checklist.map((c, n) =>
                              n === i ? { ...c, done: e.target.checked } : c,
                            ),
                          })
                        }
                      />
                      <input
                        aria-label={`Checklist item ${i + 1}`}
                        required
                        maxLength={500}
                        value={item.text}
                        onChange={(e) =>
                          setEditor({
                            ...editor,
                            checklist: editor.checklist.map((c, n) =>
                              n === i ? { ...c, text: e.target.value } : c,
                            ),
                          })
                        }
                      />
                      <button
                        type="button"
                        aria-label={`Remove checklist item ${i + 1}`}
                        onClick={() =>
                          setEditor({
                            ...editor,
                            checklist: editor.checklist.filter(
                              (_, n) => n !== i,
                            ),
                          })
                        }
                      >
                        <X size={14} />
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={() =>
                      setEditor({
                        ...editor,
                        checklist: [
                          ...editor.checklist,
                          { text: "", done: false },
                        ],
                      })
                    }
                  >
                    Add checklist item
                  </button>
                </fieldset>
                <div className="planning-settings-grid">
                  <label>
                    Worker for this task
                    <select
                      aria-label="Task worker"
                      value={editor.worker || ""}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          worker: e.target.value,
                          agent: "",
                          model: null,
                        })
                      }
                    >
                      <WorkerOptions
                        workers={workerList}
                        projectWorker={data.settings.worker}
                        current={editor.worker}
                      />
                    </select>
                  </label>
                  <label>
                    Agent for this task
                    <select
                      aria-label="Task agent"
                      value={editor.agent}
                      onChange={(e) =>
                        setEditor({ ...editor, agent: e.target.value })
                      }
                    >
                      <option value="">
                        {editorInherits
                          ? `Project default (${data.settings.agent || "worker default"})`
                          : "Worker default"}
                      </option>
                      {editor.agent &&
                        !workerAgents.some((a) => a.name === editor.agent) && (
                          <option value={editor.agent}>
                            {editor.agent} (not reported)
                          </option>
                        )}
                      {workerAgents.map((agent) => (
                        <option key={agent.name} value={agent.name}>
                          {agent.name}
                          {agent.mode && agent.mode !== "all"
                            ? ` · ${agent.mode}`
                            : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Model for this task
                    <select
                      aria-label="Task model"
                      value={
                        editor.model
                          ? JSON.stringify({
                              providerID: editor.model.providerID,
                              modelID: editor.model.modelID,
                            })
                          : ""
                      }
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          model: e.target.value
                            ? (JSON.parse(e.target.value) as ModelChoice)
                            : null,
                        })
                      }
                    >
                      <option value="">
                        {editorInherits
                          ? `Project default (${data.settings.model?.modelID || "worker default"})`
                          : `Worker default (${editorWorker?.default_model?.modelID || "unknown"})`}
                      </option>
                      {editor.model &&
                        !workerModels.some(
                          (m) =>
                            m.providerID === editor.model?.providerID &&
                            m.modelID === editor.model?.modelID,
                        ) && (
                          <option
                            value={JSON.stringify({
                              providerID: editor.model.providerID,
                              modelID: editor.model.modelID,
                            })}
                          >
                            {editor.model.providerID}/{editor.model.modelID}{" "}
                            (not reported)
                          </option>
                        )}
                      {workerModels.map((model) => (
                        <option
                          key={`${model.providerID}/${model.modelID}`}
                          value={JSON.stringify({
                            providerID: model.providerID,
                            modelID: model.modelID,
                          })}
                        >
                          {model.providerID}/{model.modelID}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Prompt approvals for this task
                    <select
                      aria-label="Task hand-off mode"
                      value={editor.handoff_mode ?? ""}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          handoff_mode: isCardHandoff(e.target.value)
                            ? e.target.value
                            : "",
                        })
                      }
                    >
                      {CARD_HANDOFF_CHOICES.map((choice) => (
                        <option key={choice || "inherit"} value={choice}>
                          {cardHandoffLabel(choice, data.settings.handoff_mode)}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <p className="planning-hint">
                  {
                    handoffCopy(
                      effectiveHandoffMode(
                        editor.handoff_mode,
                        data.settings.handoff_mode,
                      ),
                    ).summary
                  }{" "}
                  {
                    handoffCopy(
                      effectiveHandoffMode(
                        editor.handoff_mode,
                        data.settings.handoff_mode,
                      ),
                    ).escalation
                  }
                </p>
                <label>
                  Default instructions for this card
                  <textarea
                    rows={3}
                    maxLength={8000}
                    value={editor.instructions}
                    onChange={(e) =>
                      setEditor({ ...editor, instructions: e.target.value })
                    }
                  />
                </label>
                <footer>
                  <button disabled={busy} type="submit">
                    Save card
                  </button>
                  {editor.id && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void mutate(
                          { operation: "archive", card_id: editor.id },
                          editorRevision,
                        ).then((ok) => {
                          if (ok) setEditor(null);
                        })
                      }
                    >
                      Archive
                    </button>
                  )}
                  <button type="button" onClick={() => setEditor(null)}>
                    Cancel
                  </button>
                </footer>
                {error && <p role="alert">{error}</p>}
              </form>
            </section>
          </div>
        )}
        {launch && (
          <div className="planning-overlay">
            <section
              className="planning-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="launch-title"
            >
              <header>
                <h2 id="launch-title">Run: {launch.title}</h2>
                <button
                  aria-label="Close launch"
                  onClick={() => setLaunch(null)}
                >
                  <X size={18} />
                </button>
              </header>
              <label>
                Worker
                <select
                  aria-label="Launch worker"
                  disabled={busy}
                  value={launch.worker || ""}
                  onChange={(e) => chooseLaunchWorker(e.target.value)}
                >
                  <WorkerOptions
                    workers={workerList}
                    projectWorker={data.settings.worker}
                    current={launch.worker}
                  />
                </select>
              </label>
              <p>
                {launchWorker || "No worker configured"} ·{" "}
                {data.settings.directory || "Worker default directory"} ·{" "}
                {launch.agent ||
                  (launchInherits && data.settings.agent) ||
                  "Worker default agent"}{" "}
                ·{" "}
                {(launch.model || (launchInherits ? data.settings.model : null))
                  ?.modelID || "Worker default model"}
              </p>
              <label>
                Prompt template
                <select
                  aria-label="Prompt template"
                  value={template}
                  onChange={(e) => setTemplate(e.target.value)}
                >
                  <option value="">None</option>
                  {data.settings.templates.map((t) => (
                    <option key={t.name} value={t.text}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                What should the agent do?
                <textarea
                  autoFocus
                  rows={5}
                  maxLength={8000}
                  value={instructions}
                  onChange={(e) => setInstructions(e.target.value)}
                />
              </label>
              <details>
                <summary>Preview context</summary>
                <pre>
                  {JSON.stringify(
                    {
                      project: slug,
                      context: data.settings.context,
                      standingInstructions: data.settings.instructions,
                      card: launch,
                      template,
                      instructions,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
              <p>
                Project settings are captured when you add this task; card edits
                still apply until it starts. Previous completed queue results
                are added when its session starts.
              </p>
              <footer>
                {["enqueue", "run"].map((operation) => (
                  <button
                    key={operation}
                    disabled={
                      busy ||
                      !launchWorker ||
                      (operation === "run" && (!!active || !!queued.length))
                    }
                    onClick={() =>
                      void mutate({
                        operation,
                        card_id: launch.id,
                        instructions,
                        template,
                      }).then((ok) => {
                        if (ok) {
                          setLaunch(null);
                          setTab("queue");
                        }
                      })
                    }
                  >
                    {operation === "run" ? "Start session" : "Add to queue"}
                  </button>
                ))}
              </footer>
              {error && <p role="alert">{error}</p>}
            </section>
          </div>
        )}
      </fieldset>
    </>
  );
}

function editableCard(card: Card) {
  return {
    id: card.id,
    title: card.title,
    description: card.description,
    column: card.column,
    position: card.position,
    labels: [...new Set(card.labels.filter(Boolean))],
    checklist: card.checklist,
    instructions: card.instructions,
    worker: card.worker || "",
    agent: card.agent,
    model: card.model,
    // Must be sent explicitly: the server treats an absent field as "unchanged",
    // so omitting it here silently discarded the user's choice (#137).
    // "" is the real value for "inherit the project default", not a missing one.
    handoff_mode: card.handoff_mode ?? "",
  };
}

/** Options for a per-card worker select; blank means the project default. */
interface PipeStageDraft {
  id?: number;
  name: string;
  role: string;
  instructions: string;
  worker: string;
  agent: string;
  /** provider/model from the selected worker's catalog; empty is its default. */
  model: string;
  directory: string;
  requires_approval: boolean;
  status?: string;
  /** Completed and running stages can never be changed. */
  immutable?: boolean;
  /** The stage already queued to run next cannot be reordered. */
  lockedOrder?: boolean;
}

const PIPE_ROLES = ["plan", "execute", "test", "custom"] as const;
const PIPE_ACTIVE = ["queued", "running", "awaiting_approval"];

function defaultPipeStages(): PipeStageDraft[] {
  return [
    {
      name: "Plan",
      role: "plan",
      instructions:
        "Produce the implementation plan and hand it to the execute stage.",
      worker: "",
      agent: "",
      model: "",
      directory: "",
      requires_approval: true,
    },
    {
      name: "Execute",
      role: "execute",
      instructions:
        "Implement the plan artifact. Report concrete files, behavior and checks.",
      worker: "",
      agent: "",
      model: "",
      directory: "",
      requires_approval: false,
    },
    {
      name: "Test",
      role: "test",
      instructions:
        "Verify the implementation artifact and report the test result.",
      worker: "",
      agent: "",
      model: "",
      directory: "",
      requires_approval: false,
    },
  ];
}

function editPipeStages(pipe: PipeView): PipeStageDraft[] {
  return pipe.stages.map((stage) => ({
    id: stage.id,
    name: stage.name,
    role: stage.role,
    instructions: stage.instructions,
    worker: stage.worker,
    agent: stage.agent,
    model: stage.model
      ? `${stage.model.providerID}/${stage.model.modelID}`
      : "",
    directory: stage.directory,
    requires_approval: stage.requires_approval,
    status: stage.status,
    immutable: stage.status === "completed" || stage.status === "running",
    lockedOrder:
      stage.status === "completed" ||
      stage.status === "running" ||
      stage.status === "queued",
  }));
}

function stageModel(value: string): ModelChoice | null {
  const [providerID, ...rest] = value.split("/");
  return value && rest.length ? { providerID, modelID: rest.join("/") } : null;
}

function PipesPanel({
  pipes,
  workers,
  busy,
  canWrite,
  mutate,
  openTask,
  session,
}: {
  pipes: PipeView[];
  workers: Worker[];
  busy: boolean;
  canWrite: boolean;
  mutate: (
    values: Record<string, unknown>,
    revision?: number,
  ) => Promise<boolean>;
  openTask: (id: number) => void;
  session: (stage: PipeStageView) => void;
}) {
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<PipeView | null>(null);
  const close = () => {
    setCreating(false);
    setEditing(null);
  };
  return (
    <div className="planning-pipes">
      <p>
        A pipe runs an ordered plan → execute → test sequence on the same
        durable queue. Each stage can use its own worker and model, receives the
        previous stage&apos;s report artifact, and only an explicit task done
        advances it. Stage jobs show here and in the Queue tab.
      </p>
      <div className="planning-pipe-toolbar">
        <button
          disabled={!canWrite || busy}
          onClick={() => {
            setEditing(null);
            setCreating((value) => !value);
          }}
        >
          <Plus size={16} /> {creating ? "Close" : "New pipe"}
        </button>
      </div>
      {creating && canWrite && (
        <PipeForm
          key="new"
          initial={null}
          workers={workers}
          busy={busy}
          close={close}
          submit={async (pipe) => {
            const ok = await mutate({ operation: "pipe_create", pipe });
            if (ok) close();
            return ok;
          }}
        />
      )}
      {editing && canWrite && (
        <PipeForm
          key={editing.id}
          initial={editing}
          workers={workers}
          busy={busy}
          close={close}
          submit={async (pipe) => {
            const ok = await mutate({
              operation: "pipe_edit",
              pipe_edit: pipe,
            });
            if (ok) close();
            return ok;
          }}
        />
      )}
      {!pipes.length && <p>No pipes yet. Create one above.</p>}
      {pipes.map((pipe) => (
        <article className={`planning-pipe ${pipe.status}`} key={pipe.id}>
          <header>
            <div>
              <strong>{pipe.title}</strong>
              <small>
                {pipe.status.replaceAll("_", " ")} ·{" "}
                {pipe.on_failure === "loop"
                  ? `loop on test failure (${pipe.retries_used}/${pipe.max_retries} retries)`
                  : "stops for a human on failure"}
                {pipe.approved_stage
                  ? ` · approved through stage ${pipe.approved_stage}`
                  : ""}
              </small>
              {pipe.description && <p>{pipe.description}</p>}
              {pipe.detail && <p>{pipe.detail}</p>}
            </div>
            <div className="planning-pipe-actions">
              {pipe.status === "awaiting_approval" && (
                <button
                  disabled={busy}
                  onClick={() =>
                    void mutate({
                      operation: "pipe_approve",
                      pipe_id: pipe.id,
                    })
                  }
                >
                  Approve checkpoint
                </button>
              )}
              {PIPE_ACTIVE.includes(pipe.status) && (
                <button
                  disabled={busy}
                  onClick={() => {
                    setCreating(false);
                    setEditing(pipe);
                  }}
                >
                  Edit pipe
                </button>
              )}
              {!["completed", "cancelled", "failed"].includes(pipe.status) && (
                <button
                  disabled={busy}
                  onClick={() =>
                    void mutate({ operation: "pipe_stop", pipe_id: pipe.id })
                  }
                >
                  <Square size={14} /> Stop pipe
                </button>
              )}
              <button
                disabled={busy}
                onClick={() =>
                  void mutate({ operation: "pipe_remove", pipe_id: pipe.id })
                }
              >
                {PIPE_ACTIVE.includes(pipe.status)
                  ? "Remove pipe"
                  : "Remove from list"}
              </button>
            </div>
          </header>
          <ol className="planning-pipe-stages">
            {pipe.stages.map((stage) => (
              <li
                className={`planning-pipe-stage ${stage.status}`}
                key={stage.id}
              >
                <div>
                  <strong>
                    {stage.position}. {stage.name}
                  </strong>
                  <small>
                    {stage.role} · {stage.status} · {stage.worker}
                    {stage.model
                      ? ` · ${stage.model.modelID}`
                      : " · worker default model"}
                    {stage.requires_approval ? " · checkpoint after" : ""}
                  </small>
                  <small>
                    {stage.directory}
                    {stage.attempts > 1 ? ` · attempt ${stage.attempts}` : ""}
                  </small>
                  {stage.task_status && <small>task {stage.task_status}</small>}
                </div>
                <div className="planning-pipe-actions">
                  {stage.task && (
                    <button onClick={() => openTask(stage.task!)}>
                      Task #{stage.task}
                    </button>
                  )}
                  {stage.session && (
                    <button onClick={() => session(stage)}>Open session</button>
                  )}
                </div>
                {stage.artifact && (
                  <details>
                    <summary>Artifact</summary>
                    <pre>{stage.artifact}</pre>
                  </details>
                )}
              </li>
            ))}
          </ol>
        </article>
      ))}
    </div>
  );
}

function PipeForm({
  initial,
  workers,
  busy,
  close,
  submit,
}: {
  initial: PipeView | null;
  workers: Worker[];
  busy: boolean;
  close: () => void;
  submit: (pipe: Record<string, unknown>) => Promise<boolean>;
}) {
  const editing = !!initial;
  const [title, setTitle] = useState(initial?.title || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [onFailure, setOnFailure] = useState<"stop" | "loop">(
    initial?.on_failure || "stop",
  );
  const [maxRetries, setMaxRetries] = useState(initial?.max_retries ?? 1);
  const [play, setPlay] = useState(true);
  const [stages, setStages] = useState<PipeStageDraft[]>(
    initial ? editPipeStages(initial) : defaultPipeStages(),
  );
  const [error, setError] = useState("");
  const update = (index: number, changes: Partial<PipeStageDraft>) =>
    setStages((current) =>
      current.map((stage, position) =>
        position === index ? { ...stage, ...changes } : stage,
      ),
    );
  const canMove = (index: number, direction: number) => {
    const target = index + direction;
    if (target < 0 || target >= stages.length) return false;
    const locked = (stage: PipeStageDraft) =>
      Boolean(stage.immutable || stage.lockedOrder);
    return !locked(stages[index]) && !locked(stages[target]);
  };
  const move = (index: number, direction: number) => {
    if (!canMove(index, direction)) return;
    setStages((current) => {
      const next = [...current];
      const target = index + direction;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };
  return (
    <form
      className="planning-pipe-form"
      onSubmit={(event) => {
        event.preventDefault();
        setError("");
        if (!stages.length) {
          setError("Add at least one stage.");
          return;
        }
        const payloadStages = stages.map((stage) => {
          if (editing && stage.immutable) return { id: stage.id };
          return {
            ...(stage.id ? { id: stage.id } : {}),
            name: stage.name,
            role: stage.role,
            instructions: stage.instructions,
            worker: stage.worker,
            agent: stage.agent,
            model: stageModel(stage.model),
            directory: stage.directory,
            requires_approval: stage.requires_approval,
          };
        });
        const shared = {
          title,
          description,
          on_failure: onFailure,
          max_retries: maxRetries,
          stages: payloadStages,
        };
        void submit(
          editing ? { pipe_id: initial.id, ...shared } : { ...shared, play },
        );
      }}
    >
      <h3>{editing ? `Edit pipe: ${initial?.title}` : "New pipe"}</h3>
      <label>
        Pipe title
        <input
          required
          maxLength={300}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Ship the login feature"
        />
      </label>
      <label>
        Shared description / context
        <textarea
          rows={3}
          maxLength={12000}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </label>
      <div className="planning-settings-grid">
        <label>
          When the test stage fails
          <select
            value={onFailure}
            onChange={(event) =>
              setOnFailure(event.target.value as "stop" | "loop")
            }
          >
            <option value="stop">Stop and ask a human</option>
            <option value="loop">Loop back to execute</option>
          </select>
        </label>
        <label>
          Bounded retries
          <input
            type="number"
            min={0}
            max={5}
            disabled={onFailure !== "loop"}
            value={maxRetries}
            onChange={(event) => setMaxRetries(Number(event.target.value))}
          />
        </label>
        {!editing && (
          <label className="planning-inline-check">
            <input
              type="checkbox"
              checked={play}
              onChange={(event) => setPlay(event.target.checked)}
            />
            Play the queue immediately
          </label>
        )}
      </div>
      <fieldset>
        <legend>Stages</legend>
        {stages.map((stage, index) => {
          const worker = workers.find((item) => item.name === stage.worker);
          if (editing && stage.immutable) {
            return (
              <div
                className="planning-pipe-stage-editor planning-pipe-stage-locked"
                key={stage.id}
              >
                <strong>
                  {index + 1}. {stage.name}
                </strong>
                <small>
                  {stage.status} · {stage.worker}
                  {stage.model
                    ? ` · ${stage.model}`
                    : " · worker default model"}
                </small>
                <small>
                  Locked: completed and running stages cannot be edited,
                  reordered or removed.
                </small>
              </div>
            );
          }
          return (
            <div className="planning-pipe-stage-editor" key={stage.id || index}>
              <div className="planning-settings-grid">
                <label>
                  Name
                  <input
                    required
                    maxLength={100}
                    value={stage.name}
                    onChange={(event) =>
                      update(index, { name: event.target.value })
                    }
                  />
                </label>
                <label>
                  Role
                  <select
                    value={stage.role}
                    onChange={(event) =>
                      update(index, { role: event.target.value })
                    }
                  >
                    {PIPE_ROLES.map((role) => (
                      <option key={role} value={role}>
                        {role}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Worker
                  <select
                    value={stage.worker}
                    onChange={(event) =>
                      update(index, {
                        worker: event.target.value,
                        agent: "",
                        model: "",
                      })
                    }
                  >
                    <option value="">Project default</option>
                    {workers.map((item) => (
                      <option key={item.name} value={item.name}>
                        {item.name} · {item.online ? "online" : "offline"}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Model
                  <select
                    value={stage.model}
                    onChange={(event) =>
                      update(index, { model: event.target.value })
                    }
                  >
                    <option value="">Worker default</option>
                    {(worker?.models || []).map((model) => {
                      const value = `${model.providerID}/${model.modelID}`;
                      return (
                        <option key={value} value={value}>
                          {model.name || value}
                        </option>
                      );
                    })}
                  </select>
                </label>
                <label>
                  Directory
                  <input
                    maxLength={500}
                    value={stage.directory}
                    placeholder="Project or worker default"
                    onChange={(event) =>
                      update(index, { directory: event.target.value })
                    }
                  />
                </label>
              </div>
              <label>
                Stage instructions
                <textarea
                  rows={2}
                  maxLength={8000}
                  value={stage.instructions}
                  onChange={(event) =>
                    update(index, { instructions: event.target.value })
                  }
                />
              </label>
              <div className="planning-pipe-actions">
                <label className="planning-inline-check">
                  <input
                    type="checkbox"
                    checked={stage.requires_approval}
                    onChange={(event) =>
                      update(index, {
                        requires_approval: event.target.checked,
                      })
                    }
                  />
                  Pause for my approval after this stage
                </label>
                <button
                  type="button"
                  disabled={stages.length < 2}
                  aria-label={`Move ${stage.name} up`}
                  onClick={() => move(index, -1)}
                >
                  <ArrowUp size={14} />
                </button>
                <button
                  type="button"
                  disabled={stages.length < 2}
                  aria-label={`Move ${stage.name} down`}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDown size={14} />
                </button>
                {stages.length > 1 && (
                  <button
                    type="button"
                    onClick={() =>
                      setStages((current) =>
                        current.filter((_, position) => position !== index),
                      )
                    }
                  >
                    <X size={14} /> Remove stage
                  </button>
                )}
              </div>
              {stage.lockedOrder && (
                <small>
                  This stage is already queued to run next, so its order is
                  fixed. You can still edit its worker, model and instructions.
                </small>
              )}
            </div>
          );
        })}
        <button
          type="button"
          disabled={stages.length >= 10}
          onClick={() =>
            setStages((current) => [
              ...current,
              {
                name: `Stage ${current.length + 1}`,
                role: "custom",
                instructions: "",
                worker: "",
                agent: "",
                model: "",
                directory: "",
                requires_approval: false,
              },
            ])
          }
        >
          <Plus size={14} /> Add stage
        </button>
      </fieldset>
      {error && (
        <p className="planning-error" role="alert">
          {error}
        </p>
      )}
      <footer>
        <button type="submit" disabled={busy || !title.trim()}>
          {busy ? "Saving…" : editing ? "Save pipe" : "Create pipe"}
        </button>
        <button type="button" disabled={busy} onClick={close}>
          Cancel
        </button>
      </footer>
    </form>
  );
}

function WorkerOptions({
  workers,
  projectWorker,
  current,
}: {
  workers: Worker[];
  projectWorker: string;
  current?: string;
}) {
  return (
    <>
      <option value="">
        {projectWorker
          ? `Project default (${projectWorker})`
          : "Project default (none configured)"}
      </option>
      {current && !workers.some((w) => w.name === current) && (
        <option value={current}>{current} (not reported)</option>
      )}
      {workers.map((w) => (
        <option key={w.name} value={w.name}>
          {w.name} · {w.online ? "online" : "offline"}
        </option>
      ))}
    </>
  );
}

function SettingsForm({
  initial,
  revision,
  workers,
  busy,
  save,
}: {
  initial: SettingsData;
  revision: number;
  workers: Worker[];
  busy: boolean;
  save: (settings: SettingsData, revision: number) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState(initial);
  const [base, setBase] = useState(revision);
  const [saved, setSaved] = useState(false);
  const [columns, setColumns] = useState(initial.columns.join("\n"));
  const worker = workers.find((w) => w.name === draft.worker);
  return (
    <form
      className="planning-settings"
      onSubmit={(e) => {
        e.preventDefault();
        void save(
          {
            ...draft,
            columns: columns
              .split("\n")
              .map((c) => c.trim())
              .filter(Boolean),
          },
          base,
        ).then((ok) => {
          if (ok) {
            setSaved(true);
            setBase(revision + 1);
          }
        });
      }}
    >
      <h2>Project configuration</h2>
      <p>
        Defaults are inherited by new launches. Existing queued jobs retain
        their captured project settings.
      </p>
      <div className="planning-settings-grid">
        <label>
          Default worker
          <select
            aria-label="Default worker"
            value={draft.worker}
            onChange={(e) => {
              const w = workers.find((w) => w.name === e.target.value);
              setDraft({
                ...draft,
                worker: e.target.value,
                directory: w?.create_directory || w?.directory || "",
                model: null,
                agent: "",
              });
            }}
          >
            <option value="">Choose a worker</option>
            {workers.map((w) => (
              <option key={w.name} value={w.name}>
                {w.name} · {w.online ? "online" : "offline"}
              </option>
            ))}
          </select>
        </label>
        <label>
          Default agent
          <select
            aria-label="Default agent"
            value={draft.agent}
            onChange={(e) => setDraft({ ...draft, agent: e.target.value })}
          >
            <option value="">Worker default</option>
            {(worker?.agents || []).map((agent) => (
              <option key={agent.name} value={agent.name}>
                {agent.name}
                {agent.mode && agent.mode !== "all" ? ` · ${agent.mode}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          Default directory
          <input
            maxLength={500}
            placeholder="Absolute path on the selected worker"
            value={draft.directory}
            onChange={(e) => setDraft({ ...draft, directory: e.target.value })}
          />
        </label>
        <label>
          Default model
          <select
            aria-label="Default model"
            value={
              draft.model
                ? JSON.stringify({
                    providerID: draft.model.providerID,
                    modelID: draft.model.modelID,
                  })
                : ""
            }
            onChange={(e) =>
              setDraft({
                ...draft,
                model: e.target.value
                  ? (JSON.parse(e.target.value) as ModelChoice)
                  : null,
              })
            }
          >
            <option value="">Worker default</option>
            {worker?.models.map((m) => (
              <option
                key={`${m.providerID}/${m.modelID}`}
                value={JSON.stringify({
                  providerID: m.providerID,
                  modelID: m.modelID,
                })}
              >
                {m.providerID}/{m.modelID}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        Prompt approvals — the default for cards launched from this project
        <select
          aria-label="Project hand-off mode"
          value={draft.handoff_mode}
          onChange={(e) =>
            setDraft({
              ...draft,
              handoff_mode: normalizeHandoffMode(e.target.value),
            })
          }
        >
          {HANDOFF_MODES.map((mode) => (
            <option key={mode} value={mode}>
              {handoffCopy(mode).label}
            </option>
          ))}
        </select>
      </label>
      <p className="planning-hint">
        {handoffCopy(draft.handoff_mode).summary}{" "}
        {handoffCopy(draft.handoff_mode).escalation}
      </p>
      <label>
        Project context
        <textarea
          rows={5}
          maxLength={8000}
          value={draft.context}
          onChange={(e) => setDraft({ ...draft, context: e.target.value })}
        />
      </label>
      <label>
        Standing instructions — included in every session
        <textarea
          rows={5}
          maxLength={8000}
          value={draft.instructions}
          onChange={(e) => setDraft({ ...draft, instructions: e.target.value })}
        />
      </label>
      <label>
        Board columns — one per line
        <textarea
          rows={5}
          value={columns}
          onChange={(e) => setColumns(e.target.value)}
        />
      </label>
      <h3>Reusable prompts</h3>
      {draft.templates.map((t, i) => (
        <fieldset key={i}>
          <label>
            Prompt name
            <input
              required
              maxLength={100}
              value={t.name}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  templates: draft.templates.map((p, n) =>
                    i === n ? { ...p, name: e.target.value } : p,
                  ),
                })
              }
            />
          </label>
          <label>
            Prompt text
            <textarea
              required
              maxLength={8000}
              rows={3}
              value={t.text}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  templates: draft.templates.map((p, n) =>
                    i === n ? { ...p, text: e.target.value } : p,
                  ),
                })
              }
            />
          </label>
          <button
            type="button"
            onClick={() =>
              setDraft({
                ...draft,
                templates: draft.templates.filter((_, n) => n !== i),
              })
            }
          >
            Remove prompt
          </button>
        </fieldset>
      ))}
      <footer>
        <button
          type="button"
          onClick={() =>
            setDraft({
              ...draft,
              templates: [...draft.templates, { name: "", text: "" }],
            })
          }
        >
          Add prompt
        </button>
        <button disabled={busy} type="submit">
          Save project settings
        </button>
        <button
          type="button"
          onClick={() => {
            setDraft(initial);
            setColumns(initial.columns.join("\n"));
            setBase(revision);
            setSaved(false);
          }}
        >
          Reload saved settings
        </button>
        {saved && <span role="status">Settings saved</span>}
      </footer>
    </form>
  );
}

function TaskStatusEditor({ task, status }: { task: number; status: Status }) {
  return (
    <details className="planning-task-status">
      <summary>Execution status: {LABELS[status]}</summary>
      <TaskStatusForm key={`${task}:${status}`} task={task} status={status} />
    </details>
  );
}

function TaskStatusForm({ task, status }: { task: number; status: Status }) {
  const client = useQueryClient();
  const [target, setTarget] = useState<Status>(status);
  const [summary, setSummary] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="planning-completion"
      onSubmit={(e) => {
        e.preventDefault();
        if (busy || target === status) return;
        setBusy(true);
        setError("");
        void post(
          `/api/tasks/${task}/${target === "pending" ? "release" : "status"}`,
          {
            status: target,
            reason: summary,
            result_summary: summary,
          },
        )
          .then(async () => {
            setSummary("");
            await refreshTaskViews(client);
          })
          .catch((e) =>
            setError(e instanceof Error ? e.message : "Status update failed"),
          )
          .finally(() => setBusy(false));
      }}
    >
      <label>
        Task #{task} status (also updates All Tasks)
        <select
          aria-label={`Execution status for task ${task}`}
          value={target}
          disabled={busy}
          onChange={(event) => setTarget(event.target.value as Status)}
        >
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      <label>
        {target === "blocked" ? "Blocker reason" : "Outcome / handoff"}
        <textarea
          required={["blocked", "review", "done"].includes(target)}
          disabled={busy}
          rows={2}
          maxLength={5000}
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          placeholder="Verified outcome and context for the next task"
        />
      </label>
      <p>In progress requires an existing claim. Pending releases the claim.</p>
      <button
        disabled={
          busy ||
          target === status ||
          (["blocked", "review", "done"].includes(target) && !summary.trim())
        }
      >
        {busy ? "Saving…" : "Update task status"}
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
