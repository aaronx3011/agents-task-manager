// Task filters shared by the board, list and workspace summary, matching the
// web's query parameters (frontend/src/workspace-query.ts).
import type { paths } from "@/api/schema";

export type TaskQuery = NonNullable<paths["/api/v1/tasks"]["get"]["parameters"]["query"]>;
export type Status = NonNullable<TaskQuery["status"]>;
export type Ordering = NonNullable<TaskQuery["ordering"]>;

export interface Filters {
  q: string;
  project: string;
  claimed_by: string;
  status: Status | "";
  ready: "" | "true" | "false";
}

export const EMPTY_FILTERS: Filters = { q: "", project: "", claimed_by: "", status: "", ready: "" };

export const LANES: Status[] = ["pending", "in_progress", "blocked", "review", "done"];

export const ORDERINGS: { value: Ordering; label: string }[] = [
  { value: "priority", label: "Priority (low first)" },
  { value: "-priority", label: "Priority (high first)" },
  { value: "-updated_at", label: "Recently updated" },
  { value: "updated_at", label: "Least recently updated" },
  { value: "title", label: "Title A–Z" },
  { value: "project", label: "Project" },
  { value: "status", label: "Status" },
  { value: "claimed_by", label: "Agent" },
];

/** Drop empty values; the server validates the rest. */
export function taskQuery(filters: Filters, extra: Partial<TaskQuery> = {}): TaskQuery {
  const entries = Object.entries({ ...filters, ...extra }).filter(
    ([, value]) => value !== "" && value !== undefined,
  );
  return Object.fromEntries(entries) as TaskQuery;
}

export function activeFilters(filters: Filters) {
  return Object.entries(filters).filter(([key, value]) => key !== "q" && value !== "").length;
}
