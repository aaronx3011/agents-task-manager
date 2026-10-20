import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  Clock3,
  FileText,
  GitBranch,
  ListFilter,
  MessageSquare,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { get } from "./api";
import type { EventPage, TaskDetail, TaskEvent } from "./types";
import {
  Age,
  Avatar,
  Empty,
  ErrorState,
  Loading,
  Priority,
  StatusBadge,
} from "./ui";

function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
function Timeline({ id }: { id: number }) {
  const events = useInfiniteQuery({
    queryKey: ["events", id],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      get<EventPage>(
        `/api/tasks/${id}/events${pageParam ? `?before=${pageParam}` : ""}`,
      ),
    getNextPageParam: (page) => page.next_before ?? undefined,
  });
  if (events.isLoading) return <Loading label="Loading history" />;
  if (events.error)
    return (
      <ErrorState error={events.error} retry={() => void events.refetch()} />
    );
  const items = events.data?.pages.flatMap((page) => page.items) || [];
  return (
    <>
      <div className="timeline">
        {items.map((event) => (
          <EventRow key={event.id} event={event} />
        ))}
      </div>
      {events.hasNextPage && (
        <button
          className="load-more"
          disabled={events.isFetchingNextPage}
          onClick={() => void events.fetchNextPage()}
        >
          Load earlier activity
        </button>
      )}
      {!items.length && <Empty title="No activity yet" />}
    </>
  );
}
function EventRow({ event }: { event: TaskEvent }) {
  return (
    <article className="timeline-event">
      <span className={`timeline-icon event-${event.event_type}`}>
        {event.event_type === "note" ? (
          <MessageSquare size={13} />
        ) : event.event_type === "heartbeat" ? (
          <Clock3 size={13} />
        ) : (
          <Check size={13} />
        )}
      </span>
      <div>
        <strong>
          {event.agent || "System / human"}
          <span>{event.event_type.replaceAll("_", " ")}</span>
        </strong>
        {event.message && <p>{event.message}</p>}
        <Age value={event.created_at} />
      </div>
    </article>
  );
}
export function TaskModal({
  rawId,
  close,
  open,
  back,
  hasBack,
  trigger,
}: {
  rawId: string;
  close: () => void;
  open: (id: number) => void;
  back: () => void;
  hasBack: boolean;
  trigger: RefObject<HTMLElement | null>;
}) {
  const id = Number(rawId);
  const valid = Number.isSafeInteger(id) && id > 0;
  const [tab, setTab] = useState("overview");
  // Prerequisite navigation switches the task inside this mounted dialog.
  useEffect(() => setTab("overview"), [id]);
  // Close through Radix's own sequence so focus restoration runs before the
  // component unmounts; closing via the URL alone would skip it.
  const [openState, setOpenState] = useState(true);
  // Focus restoration must not depend on the trigger ref surviving live board
  // re-renders: remember the opened task id and re-find its card by data.
  const openedTaskId = useRef(valid ? id : 0);
  const restoreFocus = () => {
    const originalId = openedTaskId.current;
    const attempt = (remaining: number) => {
      const candidate =
        trigger.current?.isConnected && trigger.current.getClientRects().length
          ? trigger.current
          : document.querySelector<HTMLElement>(
              `[data-task-id="${originalId}"]`,
            );
      if (candidate?.getClientRects().length) {
        candidate.focus({ preventScroll: true });
        return;
      }
      if (remaining > 0) requestAnimationFrame(() => attempt(remaining - 1));
      else
        document
          .getElementById("workspace-title")
          ?.focus({ preventScroll: true });
    };
    attempt(6);
  };
  const task = useQuery({
    queryKey: ["task", id],
    queryFn: () => get<TaskDetail>(`/api/tasks/${id}`),
    enabled: valid,
    retry: false,
  });
  const data = task.data;
  return (
    <Dialog.Root
      open={openState}
      onOpenChange={(value) => {
        if (!value) setOpenState(false);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="modal-overlay" />
        <Dialog.Content
          className="task-modal"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            restoreFocus();
            // Deferred: calling parent navigation synchronously from Radix's
            // cleanup unmounts this subtree mid-commit and crashes React.
            setTimeout(close, 0);
          }}
        >
          <div className="modal-toolbar">
            <div>
              {hasBack && (
                <button
                  className="icon-button"
                  aria-label="Back to previous task"
                  onClick={back}
                >
                  <ArrowLeft size={17} />
                </button>
              )}
              <span className="modal-breadcrumb">
                <FileText size={14} />
                {data?.project.replace(/^demo-/, "") || "Task"}
                <span>/</span>#{rawId}
              </span>
            </div>
            <Dialog.Close asChild>
              <button className="icon-button" aria-label="Close task details">
                <X size={19} />
              </button>
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">
            Live, read-only task details, prerequisites and audit history.
          </Dialog.Description>
          <div className="modal-heading">
            <Dialog.Title>{data?.title || "Task details"}</Dialog.Title>
            {data && (
              <div className="modal-badges">
                <StatusBadge status={data.status} />
                <Priority value={data.priority} />
                <span className="muted">Priority {data.priority}</span>
              </div>
            )}
          </div>
          <div
            className="modal-tabs"
            role="tablist"
            aria-label="Task sections"
            onKeyDown={(event) => {
              if (
                ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              ) {
                event.preventDefault();
                const next =
                  event.key === "Home"
                    ? "overview"
                    : event.key === "End"
                      ? "activity"
                      : tab === "overview"
                        ? "activity"
                        : "overview";
                setTab(next);
                document.getElementById(`${next}-tab`)?.focus();
              }
            }}
          >
            <button
              role="tab"
              id="overview-tab"
              tabIndex={tab === "overview" ? 0 : -1}
              aria-selected={tab === "overview"}
              aria-controls="overview-panel"
              onClick={() => setTab("overview")}
            >
              <FileText size={15} />
              Overview
            </button>
            <button
              role="tab"
              id="activity-tab"
              tabIndex={tab === "activity" ? 0 : -1}
              aria-selected={tab === "activity"}
              aria-controls="activity-panel"
              onClick={() => setTab("activity")}
            >
              <Clock3 size={15} />
              Activity
            </button>
            <span className="live-detail">
              <span className="live-dot" />
              Live details
            </span>
          </div>
          <div className="modal-body">
            {!valid ? (
              <Empty
                title="Invalid task link"
                description="This task ID is not valid."
              />
            ) : task.isLoading ? (
              <Loading label="Loading task details" />
            ) : task.error ? (
              <ErrorState
                error={task.error}
                retry={() => void task.refetch()}
              />
            ) : (
              data &&
              (tab === "activity" ? (
                <div
                  role="tabpanel"
                  id="activity-panel"
                  aria-labelledby="activity-tab"
                >
                  <Timeline id={id} />
                </div>
              ) : (
                <div
                  role="tabpanel"
                  id="overview-panel"
                  aria-labelledby="overview-tab"
                >
                  <div className="detail-properties">
                    <div>
                      <span>Assigned agent</span>
                      <strong>
                        {data.claimed_by ? (
                          <>
                            <Avatar name={data.claimed_by} small />
                            {data.claimed_by}
                          </>
                        ) : (
                          "Unassigned"
                        )}
                      </strong>
                    </div>
                    <div>
                      <span>Last heartbeat</span>
                      <strong>
                        {data.heartbeat_at ? (
                          <Age
                            value={data.heartbeat_at}
                            heartbeat={data.status === "in_progress"}
                          />
                        ) : (
                          "—"
                        )}
                      </strong>
                    </div>
                    <div>
                      <span>Project</span>
                      <strong>{data.project}</strong>
                    </div>
                    <div>
                      <span>Created by</span>
                      <strong>{data.created_by || "Human / system"}</strong>
                    </div>
                  </div>
                  <section className="detail-section">
                    <h3>
                      <ListFilter size={15} />
                      Description & acceptance criteria
                    </h3>
                    {data.description ? (
                      <Markdown text={data.description} />
                    ) : (
                      <p className="muted">No description provided.</p>
                    )}
                  </section>
                  {data.blocked_reason && (
                    <section className="detail-section blocker-section">
                      <h3>Blocked reason</h3>
                      <Markdown text={data.blocked_reason} />
                    </section>
                  )}
                  {data.result_summary && (
                    <section className="detail-section result-section">
                      <h3>
                        <Check size={15} />
                        Result summary
                      </h3>
                      <Markdown text={data.result_summary} />
                    </section>
                  )}
                  <section className="detail-section">
                    <h3>
                      <GitBranch size={15} />
                      Prerequisites
                      <span className="count-bubble">
                        {data.dependency_progress.done}/
                        {data.dependency_progress.total}
                      </span>
                    </h3>
                    {data.dependencies.length ? (
                      <div className="dependency-list">
                        {data.dependencies.map((dependency) => (
                          <button
                            key={dependency.id}
                            onClick={() => open(dependency.id)}
                          >
                            <StatusBadge status={dependency.status} />
                            <span>
                              <small>#{dependency.id}</small>
                              {dependency.title}
                            </span>
                            <ArrowUpRight size={15} />
                          </button>
                        ))}
                      </div>
                    ) : (
                      <p className="muted">This task has no prerequisites.</p>
                    )}
                  </section>
                  <section className="detail-section timestamps">
                    <h3>
                      <Clock3 size={15} />
                      Timestamps
                    </h3>
                    {[
                      ["Created", data.created_at],
                      ["Claimed", data.claimed_at],
                      ["Updated", data.updated_at],
                    ].map(([label, value]) => (
                      <div key={label}>
                        <span>{label}</span>
                        <time>
                          {value ? new Date(value).toLocaleString() : "—"}
                        </time>
                      </div>
                    ))}
                  </section>
                </div>
              ))
            )}
          </div>
          <footer className="modal-footer">
            <span>Changes are reported by agents in real time.</span>
            <kbd>esc</kbd> to close
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
