import { useEffect, useState } from "react";
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
import type { Page, Project } from "./types";
import type { ModelChoice, Worker } from "./opencode-types";
import { ErrorState, Loading } from "./ui";
import "./planning.css";

interface SettingsData {
  worker: string;
  directory: string;
  model: ModelChoice | null;
  context: string;
  instructions: string;
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
  task?: number | null;
  task_status?: string | null;
}
interface Job {
  id: number;
  card: number;
  title: string;
  status: string;
  position: number;
  worker: string;
  session: string;
  task: number | null;
  task_status: string | null;
  detail: string;
  prompt: string;
  snapshot: Record<string, unknown>;
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
}
type Navigate = (
  changes: Record<string, string | number | null>,
  push?: boolean,
) => void;

export default function Planning({
  projects,
  project,
  navigate,
  openTask,
}: {
  projects: Project[];
  project: string;
  navigate: Navigate;
  openTask: (id: number) => void;
}) {
  const slug = project || projects[0]?.slug || "";
  return (
    <section className="planning-page">
      <header className="planning-heading">
        <div>
          <div className="eyebrow">PLAN → EXECUTE → REVIEW</div>
          <h1>Project planning</h1>
          <p>Organize your work. Launch contextual sessions in order.</p>
        </div>
        <label>
          Project
          <select
            aria-label="Planning project"
            value={slug}
            onChange={(e) => navigate({ project: e.target.value })}
          >
            {projects.map((p) => (
              <option key={p.slug} value={p.slug}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      </header>
      {slug ? (
        <ProjectPlanning
          key={slug}
          slug={slug}
          navigate={navigate}
          openTask={openTask}
        />
      ) : (
        <p>Create a project in administration to start planning.</p>
      )}
    </section>
  );
}

function ProjectPlanning({
  slug,
  navigate,
  openTask,
}: {
  slug: string;
  navigate: Navigate;
  openTask: (id: number) => void;
}) {
  const client = useQueryClient();
  const [tab, setTab] = useState("board");
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
    queryFn: () => get<Plan>(`/api/projects/${slug}/planning`),
    refetchInterval: 5000,
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
    if (busy || revision === undefined) return false;
    setBusy(true);
    setError("");
    try {
      const result = await post<Plan>(`/api/projects/${slug}/planning`, {
        revision,
        ...values,
      });
      client.setQueryData(["planning", slug], result);
      void client.invalidateQueries({ queryKey: ["workspace"] });
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
      <Loading label="Loading project board…" />
    );
  const queued = data.jobs.filter((j) => j.status === "queued");
  const active = data.jobs.find((j) => j.status === "running");
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
        <div className="view-tabs">
          {["board", "queue", "settings"].map((t) => (
            <button key={t} aria-pressed={tab === t} onClick={() => setTab(t)}>
              {t === "settings" && <Settings size={14} />}{" "}
              {t === "queue"
                ? `Queue (${queued.length})`
                : t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        <span className="planning-status" role="status">
          {data.playing
            ? active
              ? `Running: ${active.title}`
              : "Waiting for worker sync"
            : data.pause_reason || "Queue paused"}
        </span>
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
      </div>
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
      {tab === "settings" ? (
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
                        const source = data.cards.find((c) => c.id === dragged);
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
                      {card.task && (
                        <button
                          className="planning-task-link"
                          onClick={() => openTask(card.task!)}
                        >
                          Task #{card.task} · {card.task_status}
                        </button>
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
            are independent of task execution status.
          </p>
        </>
      ) : (
        <div className="planning-queue">
          <p>
            One task at a time. Each gets a fresh session and frozen
            project/card context. Explicit task completion advances the queue;
            idle sessions do not.
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
                <pre>{job.prompt || JSON.stringify(job.snapshot, null, 2)}</pre>
              </details>
              {job.task && ["running", "failed"].includes(job.status) && (
                <CompletionForm
                  task={job.task}
                  done={() => void plan.refetch()}
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
                          checklist: editor.checklist.filter((_, n) => n !== i),
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
              <button aria-label="Close launch" onClick={() => setLaunch(null)}>
                <X size={18} />
              </button>
            </header>
            <p>
              {data.settings.worker || "No worker configured"} ·{" "}
              {data.settings.directory || "Worker default directory"} ·{" "}
              {data.settings.model?.modelID || "Worker default model"}
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
              Settings are captured when you add this task. Previous completed
              queue results are added when its session starts.
            </p>
            <footer>
              {["enqueue", "run"].map((operation) => (
                <button
                  key={operation}
                  disabled={
                    busy ||
                    !data.settings.worker ||
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
  };
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
        their captured settings.
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

function CompletionForm({ task, done }: { task: number; done: () => void }) {
  const [summary, setSummary] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="planning-completion"
      onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        setError("");
        void post(`/api/tasks/${task}/status`, {
          status: "done",
          result_summary: summary,
        })
          .then(() => {
            setSummary("");
            done();
          })
          .catch((e) =>
            setError(e instanceof Error ? e.message : "Completion failed"),
          )
          .finally(() => setBusy(false));
      }}
    >
      <label>
        Explicit completion / handoff
        <textarea
          required
          rows={2}
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          placeholder="Verified outcome and context for the next task"
        />
      </label>
      <button disabled={busy || !summary.trim()}>Mark task completed</button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
