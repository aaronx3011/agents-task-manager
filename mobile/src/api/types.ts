// Shapes for endpoints the OpenAPI schema leaves untyped (worker sessions,
// messages, planning). Kept in step with frontend/src/opencode-types.ts and
// the interfaces in frontend/src/Planning.tsx.

export interface ModelChoice {
  providerID: string;
  modelID: string;
  name?: string;
}
export interface Worker {
  name: string;
  agent: string | null;
  is_enabled: boolean;
  online: boolean;
  connected: boolean;
  stream_connected: boolean;
  hostname: string;
  opencode_version: string;
  directory: string;
  create_directory: string;
  last_error: string;
  last_sync_at: string | null;
  default_model: ModelChoice | null;
  models: ModelChoice[];
  dropped_events: number;
  counts?: {
    sessions: number;
    commands: number;
    pending: number;
    events: number;
  };
  pending_interactions?: Interaction[];
}
export interface Session {
  sessionID: string;
  worker: string;
  title: string;
  directory: string;
  agentName: string;
  model: ModelChoice | null;
  parentID: string | null;
  subagents?: string[];
  executionStatus: string;
  lastError: string | null;
  lastSeenAt: string;
  counts: { messages: number; events: number; pending: number };
  window: { count: number; complete: boolean; oldestId: string | null };
  commands?: RemoteCommand[];
  pendingInteractions?: Interaction[];
  taskLinks?: number[];
}
export interface RemoteCommand {
  id: string;
  worker: string;
  action: string;
  sessionID: string | null;
  status: string;
  deliveryUncertain: boolean;
  error?: string;
  uncertain?: string;
  createdAt: string;
  payload: Record<string, unknown>;
  result?: unknown;
}
export interface Message {
  id: number;
  remoteID: string;
  role: string;
  publishedAt: string;
  parts: unknown[];
  content: Record<string, unknown>;
}
export interface MessageWindow {
  messages: Message[];
  cursor: number;
  gap: boolean;
  hasMore: boolean;
  retentionLimit: number;
  olderCursor?: string | null;
}
export interface Question {
  header: string;
  text: string;
  options: { label: string; description: string }[];
  multiple: boolean;
  custom: boolean;
}
export interface Interaction {
  id: string;
  kind: "permission" | "question";
  sessionID: string;
  replyDispatched: boolean;
  replyCommandId?: string;
  title?: string;
  description?: string;
  type?: string;
  patterns?: unknown;
  questions?: Question[];
}
export interface CommandInput {
  worker: string;
  action: string;
  sessionID?: string;
  payload: Record<string, unknown>;
}

export interface PlanningSettings {
  worker: string;
  directory: string;
  model: ModelChoice | null;
  context: string;
  instructions: string;
  templates: { name: string; text: string }[];
  columns: string[];
}
export interface PlanningCard {
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
export interface PlanningJob {
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
export interface Plan {
  project: string;
  name: string;
  settings: PlanningSettings;
  revision: number;
  cards: PlanningCard[];
  jobs: PlanningJob[];
  playing: boolean;
  pause_reason: string;
}

export interface Page<T> {
  count: number;
  items: T[];
}

export interface LiveSnapshot {
  type: "session.live";
  worker: string;
  sessionID: string;
  epoch: string;
  sequence: number;
  executionStatus?: string;
  messages: Pick<Message, "remoteID" | "role" | "content" | "parts">[];
}

export function mergeLiveMessages(saved: Message[], snapshot: LiveSnapshot | null): Message[] {
  const messages = new Map(saved.map((message) => [message.remoteID, message]));
  for (const message of snapshot?.messages || []) {
    const previous = messages.get(message.remoteID);
    // An older sampled partial must not replace a durably completed message.
    const savedTime = previous?.content.time as { completed?: number } | undefined;
    const liveTime = message.content.time as { completed?: number } | undefined;
    if (savedTime?.completed && !liveTime?.completed) continue;
    messages.set(message.remoteID, {
      ...message,
      id: previous?.id ?? 0,
      publishedAt:
        previous?.publishedAt ??
        new Date(
          Number((message.content.time as { created?: number } | undefined)?.created) || Date.now(),
        ).toISOString(),
    });
  }
  return [...messages.values()].sort((a, b) => {
    const first = (a.content.time as { created?: number } | undefined)?.created;
    const second = (b.content.time as { created?: number } | undefined)?.created;
    return first && second ? first - second : 0;
  });
}
