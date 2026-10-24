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
