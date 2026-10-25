export const STATUSES = [
  "pending",
  "in_progress",
  "blocked",
  "review",
  "done",
  "cancelled",
] as const;
export type Status = (typeof STATUSES)[number];
export const LABELS: Record<Status, string> = {
  pending: "Pending",
  in_progress: "In progress",
  blocked: "Blocked",
  review: "In review",
  done: "Done",
  cancelled: "Cancelled",
};
export const LANES = STATUSES.filter((status) => status !== "cancelled");
export interface Task {
  id: number;
  title: string;
  description: string;
  project: string;
  status: Status;
  priority: number;
  claimed_by: string | null;
  claimed_at: string | null;
  heartbeat_at: string | null;
  depends_on: number[];
  dependency_progress: { done: number; total: number };
  blocked_reason: string;
  result_summary: string;
  is_ready: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}
export interface TaskEvent {
  id: number;
  agent: string | null;
  event_type: string;
  message: string;
  created_at: string;
}
export interface TaskDetail extends Task {
  dependencies: Task[];
  recent_events: TaskEvent[];
}
export interface Assignment {
  id: number;
  title: string;
  status: Status;
  heartbeat_at: string | null;
}
export interface Agent {
  name: string;
  is_active: boolean;
  last_seen: string | null;
  assignments: Assignment[];
}
export interface Project {
  slug: string;
  name: string;
  kind: string;
  active_count: number;
  docs_path: string;
}
export interface ActivityEvent extends TaskEvent {
  task_id: number;
  title: string;
  project: string;
}
export interface Workspace {
  server_time: string;
  counts: Record<Status, number>;
  total: number;
  ready_count: number;
  projects: Project[];
  agents: Agent[];
  events: ActivityEvent[];
}
export interface Page<T> {
  count: number;
  items: T[];
}
export interface EventPage {
  items: TaskEvent[];
  next_before: number | null;
}
export interface Filters {
  project: string;
  q: string;
  claimed_by: string;
  status: string;
  ready: string;
}
export interface LocationState {
  section: "tasks" | "opencodes" | "sessions" | "activity" | "configuration";
  worker: string;
  session: string;
  filters: Filters;
  view: "kanban" | "list";
  ordering: string;
  page: number;
  task: string;
}
