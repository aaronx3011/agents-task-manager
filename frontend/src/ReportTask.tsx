import { useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useQueryClient } from "@tanstack/react-query";
import { MessageSquarePlus, X } from "lucide-react";
import { api, unwrap } from "./api-client";
import { refreshTaskViews } from "./task-sync";
import type { Project } from "./types";
import { pmPromptTooLong, type PmAction } from "./report-pm";
import { sendReportToPm } from "./report-pm-client";
import "./report-task.css";

export function ReportTask({
  projects,
  project,
  pmAvailable = false,
}: {
  projects: Project[];
  project: string;
  // The PM router is owner-only on the server; members only see the form.
  pmAvailable?: boolean;
}) {
  const client = useQueryClient();
  const titleInput = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [selectedProject, setSelectedProject] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("100");
  const [route, setRoute] = useState<"task" | "pm">("task");
  const [pmAction, setPmAction] = useState<PmAction>("create");
  const [worker, setWorker] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || !title.trim() || !selectedProject) return;
    const viaPm = route === "pm" && pmAvailable;
    const report = {
      action: pmAction,
      project: selectedProject,
      title,
      description,
      priority: Number(priority),
      worker,
      model,
    };
    if (viaPm && pmPromptTooLong(report)) {
      setError(
        "This report is too long for the PM router (4,000 characters). Shorten the description or use the standard form.",
      );
      return;
    }
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      if (viaPm) {
        const outcome = await sendReportToPm(report);
        // The PM turn may have committed work; refresh either way.
        void refreshTaskViews(client);
        if (!outcome.ok) {
          setError(outcome.message);
          return;
        }
        setNotice(outcome.message);
        setOpen(false);
        return;
      }
      const task = await unwrap(
        api.POST("/api/v1/tasks", {
          body: {
            project: selectedProject,
            title: title.trim(),
            description: description.trim(),
            priority: Number(priority),
          },
        }),
      );
      setNotice(`Task #${task.id} created: ${task.title}`);
      setOpen(false);
      // Creation has committed; a refresh failure must not invite a duplicate.
      void refreshTaskViews(client);
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : "Task creation failed",
      );
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return (
    <>
      <Dialog.Root
        open={open}
        onOpenChange={(value) => {
          if (submitting.current) return;
          if (value) {
            setSelectedProject(
              projects.some((item) => item.slug === project)
                ? project
                : projects[0]?.slug || "",
            );
            setTitle("");
            setDescription(`Page URL: ${window.location.href}\n\n`);
            setPriority("100");
            setRoute("task");
            setPmAction("create");
            setWorker("");
            setModel("");
            setError("");
            setNotice("");
          }
          setOpen(value);
        }}
      >
        <Dialog.Trigger asChild>
          <button
            className="report-task-bubble"
            aria-label="Report — create a task"
            title="Report — create a task"
          >
            <MessageSquarePlus size={22} aria-hidden="true" />
            <span>Report</span>
          </button>
        </Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Overlay className="modal-overlay" />
          <Dialog.Content
            className="report-task-dialog"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              titleInput.current?.focus();
            }}
          >
            <header>
              <Dialog.Title>Report / create a task</Dialog.Title>
              <Dialog.Close asChild>
                <button
                  className="icon-button"
                  disabled={busy}
                  aria-label="Close report form"
                >
                  <X size={19} />
                </button>
              </Dialog.Close>
            </header>
            <Dialog.Description>
              Report an issue or request work. It will be added as a pending
              task.
            </Dialog.Description>
            <form onSubmit={(event) => void submit(event)} aria-busy={busy}>
              <fieldset disabled={busy}>
                {pmAvailable && (
                  <div
                    className="report-task-route"
                    role="radiogroup"
                    aria-label="Where to send this report"
                  >
                    <label>
                      <input
                        type="radio"
                        name="report-route"
                        checked={route === "task"}
                        onChange={() => setRoute("task")}
                      />
                      Standard task form
                    </label>
                    <label>
                      <input
                        type="radio"
                        name="report-route"
                        checked={route === "pm"}
                        onChange={() => setRoute("pm")}
                      />
                      Tag to PM router
                    </label>
                  </div>
                )}
                <label>
                  Title
                  <input
                    ref={titleInput}
                    required
                    maxLength={300}
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                  />
                </label>
                <label>
                  Project
                  <select
                    required
                    value={selectedProject}
                    onChange={(event) => setSelectedProject(event.target.value)}
                  >
                    <option value="" disabled>
                      Select a project
                    </option>
                    {projects.map((item) => (
                      <option key={item.slug} value={item.slug}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Description
                  <textarea
                    rows={8}
                    maxLength={50000}
                    placeholder="What happened, or what needs to be done?"
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                  />
                </label>
                {route === "pm" && pmAvailable && (
                  <>
                    <label>
                      PM action
                      <select
                        value={pmAction}
                        onChange={(event) =>
                          setPmAction(event.target.value as PmAction)
                        }
                      >
                        <option value="create">Create backlog task only</option>
                        <option value="queue">
                          Create, queue and play on the project queue
                        </option>
                      </select>
                    </label>
                    {pmAction === "queue" && (
                      <div className="report-task-pair">
                        <label>
                          Worker (optional)
                          <input
                            maxLength={100}
                            value={worker}
                            onChange={(event) => setWorker(event.target.value)}
                          />
                        </label>
                        <label>
                          Model (optional, provider/model)
                          <input
                            maxLength={300}
                            value={model}
                            onChange={(event) => setModel(event.target.value)}
                          />
                        </label>
                      </div>
                    )}
                  </>
                )}
                <label>
                  Priority
                  <select
                    value={priority}
                    onChange={(event) => setPriority(event.target.value)}
                  >
                    <option value="0">Urgent</option>
                    <option value="25">High</option>
                    <option value="100">Normal</option>
                    <option value="150">Low</option>
                  </select>
                </label>
              </fieldset>
              {!projects.length && (
                <p>
                  No projects available. Create a project in Planning first.
                </p>
              )}
              {error && (
                <p className="report-task-error" role="alert">
                  {error}
                </p>
              )}
              <footer>
                <Dialog.Close asChild>
                  <button
                    type="button"
                    className="button secondary"
                    disabled={busy}
                  >
                    Cancel
                  </button>
                </Dialog.Close>
                <button
                  type="submit"
                  className="button"
                  disabled={busy || !selectedProject || !title.trim()}
                >
                  {busy
                    ? route === "pm"
                      ? "Sending to PM…"
                      : "Creating…"
                    : route === "pm"
                      ? "Send to PM"
                      : "Create task"}
                </button>
              </footer>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <div className="report-task-notice" role="status">
        {notice && (
          <>
            <span>{notice}</span>
            <button
              className="icon-button"
              aria-label="Dismiss task confirmation"
              onClick={() => setNotice("")}
            >
              <X size={16} />
            </button>
          </>
        )}
      </div>
    </>
  );
}
