// Typed read models for the chat room API (GH #149). The endpoints are declared
// as free-form objects in the OpenAPI contract, so the shapes are named here and
// the generated client is still used for every request.
import { api, unwrap } from "./api-client";

export interface Participant {
  id: number;
  position: number;
  role: string;
  persona: string;
  kind: "provider" | "worker";
  model: { providerID: string; modelID: string } | null;
  worker: string | null;
  agent: string;
  canAct: boolean;
  isMuted: boolean;
}

export interface Room {
  id: number;
  project: string;
  name: string;
  topic: string;
  status: "open" | "closed";
  roundsPerTurn: number;
  createdBy: string;
  turn: number;
  turnBusy: boolean;
  pendingSpeakers: number[];
  messageCount: number;
  historyBytes: number;
  createdAt: string;
  updatedAt: string;
  participants?: Participant[];
}

export interface Message {
  id: number;
  seq: number;
  turn: number;
  authorKind: "human" | "participant" | "system";
  participantId: number | null;
  role: string;
  text: string;
  toolEvents: { tool: string; is_error: boolean }[];
  model: { providerID: string; modelID: string } | null;
  createdAt: string;
}

export interface History {
  messages: Message[];
  cursor: number;
  olderCursor: number | null;
  hasMore: boolean;
  messageCount: number;
}

export interface AdvanceResult {
  advanced: boolean;
  message: Message | null;
  room: Room;
}

export const PAGE = 20;

export async function listRooms(project?: string) {
  return (await unwrap(
    api.GET("/api/v1/chat/rooms", {
      params: { query: project ? { project } : {} },
    }),
  )) as unknown as { count: number; items: Room[] };
}

export async function readRoom(id: number) {
  return (await unwrap(
    api.GET("/api/v1/chat/rooms/{id}", { params: { path: { id } } }),
  )) as unknown as Room;
}

export async function readHistory(id: number, before?: number, after?: number) {
  const query: Record<string, string | number | boolean> = { limit: PAGE };
  if (after !== undefined) query.after = after;
  else if (before !== undefined) query.before = before;
  else query.latest = true;
  return (await unwrap(
    api.GET("/api/v1/chat/rooms/{id}/messages", {
      params: { path: { id }, query },
    }),
  )) as unknown as History;
}

export async function createRoom(body: {
  project: string;
  name: string;
  topic?: string;
}) {
  return (await unwrap(
    api.POST("/api/v1/chat/rooms", { body }),
  )) as unknown as Room;
}

export async function patchRoom(id: number, body: Record<string, unknown>) {
  return (await unwrap(
    api.PATCH("/api/v1/chat/rooms/{id}", { params: { path: { id } }, body }),
  )) as unknown as Room;
}

export async function closeRoom(id: number) {
  return (await unwrap(
    api.DELETE("/api/v1/chat/rooms/{id}", { params: { path: { id } } }),
  )) as unknown as Room;
}

export interface ParticipantDraft {
  role: string;
  persona?: string;
  kind?: "provider" | "worker";
  model?: { providerID: string; modelID: string };
  worker?: string;
  agent?: string;
  canAct?: boolean;
  isMuted?: boolean;
  position?: number;
}

export async function addParticipant(id: number, body: ParticipantDraft) {
  return (await unwrap(
    api.POST("/api/v1/chat/rooms/{id}/participants", {
      params: { path: { id } },
      body,
    }),
  )) as unknown as Participant;
}

export async function patchParticipant(
  id: number,
  participantId: number,
  body: Record<string, unknown>,
) {
  return (await unwrap(
    api.PATCH("/api/v1/chat/rooms/{id}/participants/{participant_id}", {
      params: { path: { id, participant_id: participantId } },
      body,
    }),
  )) as unknown as Participant;
}

export async function removeParticipant(id: number, participantId: number) {
  await unwrap(
    api.DELETE("/api/v1/chat/rooms/{id}/participants/{participant_id}", {
      params: { path: { id, participant_id: participantId } },
    }),
  );
}

export async function sendMessage(id: number, text: string, rounds?: number) {
  return (await unwrap(
    api.POST("/api/v1/chat/rooms/{id}/messages", {
      params: { path: { id } },
      body: rounds ? { text, rounds } : { text },
    }),
  )) as unknown as { message: Message; room: Room };
}

export async function advanceRoom(
  id: number,
  options: { rounds?: number; reset?: boolean } = {},
) {
  return (await unwrap(
    api.POST("/api/v1/chat/rooms/{id}/advance", {
      params: { path: { id } },
      body: options,
    }),
  )) as unknown as AdvanceResult;
}

export async function listModels() {
  return (await unwrap(api.GET("/api/v1/chat/models"))) as unknown as {
    models: { providerID: string; modelID: string }[];
    limits: Record<string, number | string>;
  };
}
