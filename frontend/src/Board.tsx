import { useState } from "react";
import type { CSSProperties } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ArrowDown, LockKeyhole, MoreHorizontal } from "lucide-react";
import { get, query } from "./api";
import { LABELS, LANES } from "./types";
import type { Filters, Page, Status, Task } from "./types";
import {
  Age,
  Avatar,
  DependencyBadge,
  Empty,
  ErrorState,
  Loading,
  Priority,
  ReadyMark,
  tone,
} from "./ui";

function TaskCard({
  task,
  open,
  highlighted,
}: {
  task: Task;
  open: (id: number, element?: HTMLElement) => void;
  highlighted: boolean;
}) {
  return (
    <button
      className={`task-card card-${task.status} ${highlighted ? "task-updated" : ""}`}
      data-task-id={task.id}
      onClick={(event) => open(task.id, event.currentTarget)}
      aria-label={`Open task: ${task.title}`}
    >
      <span className="card-eyebrow">
        <span className={`project-tag ${tone(task.project)}`}>
          {task.project.replace(/^demo-/, "")}
        </span>
        <span>#{task.id}</span>
      </span>
      <span className="card-title">{task.title}</span>
      {task.blocked_reason && (
        <span className="card-blocker">
          <LockKeyhole size={12} />
          {task.blocked_reason}
        </span>
      )}
      <span className="card-tags">
        <Priority value={task.priority} />
        <DependencyBadge task={task} />
        <ReadyMark task={task} />
      </span>
      <span className="card-footer">
        <span className="card-owner">
          {task.claimed_by ? (
            <>
              <Avatar name={task.claimed_by} small />
              <span>{task.claimed_by}</span>
            </>
          ) : (
            <span className="unassigned">
              {task.status === "done" ? "Completed" : "Unassigned"}
            </span>
          )}
        </span>
        <span className="card-time">
          <Age
            value={task.heartbeat_at || task.updated_at}
            heartbeat={task.status === "in_progress"}
          />
        </span>
      </span>
    </button>
  );
}
function Lane({
  status,
  filters,
  open,
  mobile,
  highlighted,
}: {
  status: Status;
  filters: Filters;
  open: (id: number, element?: HTMLElement) => void;
  mobile: boolean;
  highlighted: Set<number>;
}) {
  const tasks = useInfiniteQuery({
    queryKey: ["tasks", "lane", filters, status],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      get<Page<Task>>(
        `/api/tasks?${query(filters, { status, limit: 20, offset: pageParam, ordering: "priority" })}`,
      ),
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.items.length, 0);
      return loaded < last.count ? loaded : undefined;
    },
    enabled: !filters.status || filters.status === status,
  });
  const items = [
    ...new Map(
      (tasks.data?.pages.flatMap((page) => page.items) || []).map((task) => [
        task.id,
        task,
      ]),
    ).values(),
  ];
  const count = tasks.data?.pages[0].count || 0;
  return (
    <section
      className={`board-lane lane-${status} ${mobile ? "mobile-active" : ""}`}
      aria-label={`${LABELS[status]} tasks`}
    >
      <div className="lane-header">
        <span className={`lane-mark status-${status}`}>
          <span className="status-dot" />
        </span>
        <h2>{LABELS[status]}</h2>
        <span className="lane-count">{count}</span>
        <MoreHorizontal size={16} className="muted" aria-hidden="true" />
      </div>
      <div className="lane-items">
        {tasks.isLoading ? (
          <>
            <div className="skeleton skeleton-card" />
            <div className="skeleton skeleton-card" />
            <div className="skeleton skeleton-card short" />
          </>
        ) : tasks.error ? (
          <ErrorState error={tasks.error} retry={() => void tasks.refetch()} />
        ) : !items.length ? (
          <Empty
            title="Nothing here yet"
            description={
              status === "pending"
                ? "Ready work will appear here."
                : "Tasks arrive as agents make progress."
            }
          />
        ) : (
          items.map((task) => (
            <TaskCard
              task={task}
              key={task.id}
              open={open}
              highlighted={highlighted.has(task.id)}
            />
          ))
        )}
        {tasks.hasNextPage && (
          <button
            className="load-more"
            disabled={tasks.isFetchingNextPage}
            onClick={() => void tasks.fetchNextPage()}
          >
            {tasks.isFetchingNextPage ? (
              <Loading label="Loading" />
            ) : (
              <>
                <ArrowDown size={14} />
                Load more · {items.length} of {count}
              </>
            )}
          </button>
        )}
      </div>
      <div className="lane-footer">
        {items.length ? `${items.length} of ${count} tasks` : "All clear"}
        <span className={`tiny-dot ${items.length ? "" : "quiet"}`} />
      </div>
    </section>
  );
}
export function Board({
  filters,
  open,
  highlighted,
}: {
  filters: Filters;
  open: (id: number, element?: HTMLElement) => void;
  highlighted: Set<number>;
}) {
  const [selected, setSelected] = useState<Status>("pending");
  const mobileStatus = filters.status ? (filters.status as Status) : selected;
  const lanes = filters.status === "cancelled" ? ["cancelled" as const] : LANES;
  return (
    <div className="board-wrapper">
      <label className="mobile-lane-picker">
        Column
        <select
          aria-label="Board column"
          value={mobileStatus}
          onChange={(event) => setSelected(event.target.value as Status)}
        >
          {(filters.status ? [filters.status as Status] : lanes).map(
            (status) => (
              <option key={status} value={status}>
                {LABELS[status]}
              </option>
            ),
          )}
        </select>
      </label>
      <div
        className="kanban-board"
        style={{ "--lane-count": lanes.length } as CSSProperties}
      >
        {lanes.map((status) => (
          <Lane
            key={status}
            status={status}
            filters={filters}
            open={open}
            mobile={mobileStatus === status}
            highlighted={highlighted}
          />
        ))}
      </div>
    </div>
  );
}
