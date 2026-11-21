import { lazy, Suspense, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useQueryClient } from "@tanstack/react-query";
import { MessageSquarePlus, X } from "lucide-react";
import { api, unwrap } from "./api-client";
import { refreshTaskViews } from "./task-sync";
import type { Project } from "./types";
import "./report-task.css";

const VoiceAgent = lazy(() => import("./VoiceAgent"));

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
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const pmView = route === "pm" && pmAvailable;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || !title.trim() || !selectedProject) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
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
            {pmView && (
              <Suspense fallback={<p>Loading PM router…</p>}>
                <VoiceAgent
                  embedded
                  initialDraft={`Report for project "${selectedProject}" from ${window.location.href}: `}
                />
              </Suspense>
            )}
            <form
              hidden={pmView}
              onSubmit={(event) => void submit(event)}
              aria-busy={busy}
            >
              <fieldset disabled={busy}>
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
                  {busy ? "Creating…" : "Create task"}
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
