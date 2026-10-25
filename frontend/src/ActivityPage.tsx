import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { get } from "./api";
import { Age, Empty, ErrorState, Loading } from "./ui";
import type { ActivityEvent, Page } from "./types";
import type { RemoteCommand } from "./opencode-types";
import "./opencodes.css";

export default function ActivityPage({
  events,
  openTask,
  openSession,
}: {
  events: ActivityEvent[];
  openTask: (id: number) => void;
  openSession: (worker: string, session: string) => void;
}) {
  const [offset, setOffset] = useState(0);
  const commands = useQuery({
    queryKey: ["opencodes", "activity", offset],
    queryFn: ({ signal }) =>
      get<Page<RemoteCommand>>(
        `/api/commands?limit=30&offset=${offset}`,
        signal,
      ),
    refetchInterval: 10000,
  });
  return (
    <section className="oc-workspace" aria-label="Workspace activity">
      <header className="oc-heading">
        <div>
          <div className="eyebrow">WORKSPACE TIMELINE</div>
          <h1>
            <Activity size={22} /> Activity
          </h1>
          <p>
            Worker commands and task updates, separate from your conversations.
          </p>
        </div>
      </header>
      <section
        className="oc-activity-commands"
        aria-label="Recent worker commands"
      >
        <h2>Recent worker commands</h2>
        {commands.isLoading ? (
          <Loading />
        ) : commands.error ? (
          <ErrorState
            error={commands.error}
            retry={() => void commands.refetch()}
          />
        ) : !commands.data?.count ? (
          <Empty title="No worker commands yet" />
        ) : (
          commands.data.items.map((command) => (
            <div
              className={`oc-command ${command.deliveryUncertain || command.status === "error" ? "oc-warning" : ""}`}
              key={command.id}
            >
              <strong>
                #{command.id} · {command.action}
              </strong>
              <span>{command.worker}</span>
              <span>
                {command.deliveryUncertain
                  ? "Delivery uncertain"
                  : command.status === "done" && command.action === "prompt"
                    ? "Accepted · execution separate"
                    : command.status}
              </span>
              <Age value={command.createdAt} />
              {command.sessionID && (
                <button
                  className="oc-text-button"
                  onClick={() =>
                    openSession(command.worker, command.sessionID!)
                  }
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
          ))
        )}
        {(commands.data?.count || 0) > 30 && (
          <div className="oc-pager">
            <button
              className="button secondary"
              disabled={!offset}
              onClick={() => setOffset(Math.max(0, offset - 30))}
            >
              Previous commands
            </button>
            <span>
              {offset + 1}–{Math.min(offset + 30, commands.data?.count || 0)}
            </span>
            <button
              className="button secondary"
              disabled={offset + 30 >= (commands.data?.count || 0)}
              onClick={() => setOffset(offset + 30)}
            >
              Next commands
            </button>
          </div>
        )}
      </section>
      <section className="oc-task-activity" aria-label="Task activity">
        <h2>Task activity</h2>
        {events.map((event) => (
          <button
            className="activity-item"
            key={event.id}
            onClick={() => openTask(event.task_id)}
          >
            <div>
              <strong>
                {event.agent || "System / human"} ·{" "}
                {event.event_type.replaceAll("_", " ")}
              </strong>
              <p>{event.title}</p>
              {event.message && <small>{event.message}</small>}
              <Age value={event.created_at} />
            </div>
          </button>
        ))}
        {!events.length && <Empty title="No task activity yet" />}
      </section>
    </section>
  );
}
