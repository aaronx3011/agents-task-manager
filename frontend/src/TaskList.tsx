import { useQuery } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { get, query } from "./api";
import type { Filters, Page, Task } from "./types";
import {
  Age,
  Avatar,
  DependencyBadge,
  Empty,
  ErrorState,
  Loading,
  Priority,
  StatusBadge,
} from "./ui";

const SIZE = 30;
export function TaskList({
  filters,
  ordering,
  page,
  change,
  open,
}: {
  filters: Filters;
  ordering: string;
  page: number;
  change: (changes: Record<string, string | number | null>) => void;
  open: (id: number, element?: HTMLElement) => void;
}) {
  const tasks = useQuery({
    queryKey: ["tasks", "list", filters, ordering, page],
    queryFn: () =>
      get<Page<Task>>(
        `/api/tasks?${query(filters, { ordering, limit: SIZE, offset: page * SIZE })}`,
      ),
  });
  function heading(title: string, field: string) {
    const active = ordering.replace(/^-/, "") === field;
    const Icon = active
      ? ordering.startsWith("-")
        ? ArrowDown
        : ArrowUp
      : ArrowUpDown;
    return (
      <button
        className={`table-sort ${active ? "active" : ""}`}
        onClick={() =>
          change({
            ordering: ordering === field ? `-${field}` : field,
            page: null,
          })
        }
      >
        {title}
        <Icon size={12} />
      </button>
    );
  }
  function direction(field: string): "none" | "ascending" | "descending" {
    return ordering.replace(/^-/, "") !== field
      ? "none"
      : ordering.startsWith("-")
        ? "descending"
        : "ascending";
  }
  if (tasks.isLoading) return <Loading label="Loading tasks" />;
  if (tasks.error)
    return (
      <ErrorState error={tasks.error} retry={() => void tasks.refetch()} />
    );
  const data = tasks.data!;
  return (
    <div className="list-view">
      <div className="table-scroll">
        <table className="task-table">
          <thead>
            <tr>
              <th aria-sort={direction("title")}>{heading("Task", "title")}</th>
              <th aria-sort={direction("project")}>
                {heading("Project", "project")}
              </th>
              <th aria-sort={direction("status")}>
                {heading("Status", "status")}
              </th>
              <th aria-sort={direction("claimed_by")}>
                {heading("Agent", "claimed_by")}
              </th>
              <th aria-sort={direction("priority")}>
                {heading("Priority", "priority")}
              </th>
              <th>Dependencies</th>
              <th aria-sort={direction("updated_at")}>
                {heading("Updated", "updated_at")}
              </th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((task) => (
              <tr
                key={task.id}
                onClick={(event) =>
                  open(
                    task.id,
                    event.currentTarget.querySelector<HTMLElement>(
                      ".table-task",
                    ) || undefined,
                  )
                }
              >
                <td>
                  <button
                    className="table-task"
                    data-task-id={task.id}
                    aria-label={`Open task: ${task.title}`}
                  >
                    <span className="task-number">#{task.id}</span>
                    {task.title}
                  </button>
                </td>
                <td>
                  <span className="table-project">
                    {task.project.replace(/^demo-/, "")}
                  </span>
                </td>
                <td>
                  <StatusBadge status={task.status} />
                </td>
                <td>
                  {task.claimed_by ? (
                    <span className="table-owner">
                      <Avatar name={task.claimed_by} small />
                      {task.claimed_by}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <Priority value={task.priority} />
                </td>
                <td>
                  <DependencyBadge task={task} />
                  {!task.dependency_progress.total && (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <Age value={task.updated_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!data.items.length && (
          <Empty
            title={page > 0 ? "This page is now empty" : "No matching tasks"}
            description={
              page > 0
                ? "Live changes may have moved tasks. Return to the first page."
                : "Try a different search or clear your filters."
            }
          >
            {page > 0 && (
              <button
                className="button secondary"
                onClick={() => change({ page: null })}
              >
                First page
              </button>
            )}
          </Empty>
        )}
      </div>
      <div className="list-pagination">
        <span>
          {data.count ? Math.min(page * SIZE + 1, data.count) : 0}–
          {Math.min((page + 1) * SIZE, data.count)} of {data.count} tasks
        </span>
        <div>
          <button
            className="button secondary icon-button"
            aria-label="Previous page"
            disabled={page === 0}
            onClick={() => change({ page: page - 1 })}
          >
            <ChevronLeft size={16} />
          </button>
          <span>Page {page + 1}</span>
          <button
            className="button secondary icon-button"
            aria-label="Next page"
            disabled={(page + 1) * SIZE >= data.count}
            onClick={() => change({ page: page + 1 })}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}
