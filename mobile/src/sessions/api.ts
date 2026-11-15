// Worker, session and command data for the Sessions tab (#78), mirroring the
// queries in frontend/src/OpenCodes.tsx.
import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type {
  CommandInput,
  MessageWindow,
  ModelChoice,
  Page,
  RemoteCommand,
  Session,
  Worker,
} from "@/api/types";
import { useSession } from "@/auth/session";
import { requestId } from "@/ui/format";

export function usable(worker: Worker) {
  return worker.is_enabled && worker.online && worker.connected && worker.stream_connected;
}

export function health(worker: Worker) {
  return !worker.is_enabled
    ? "Disabled"
    : !worker.online
      ? "Bridge offline"
      : !worker.connected
        ? "OpenCode unreachable"
        : !worker.stream_connected
          ? "Event stream reconnecting"
          : "Connected";
}

export const modelKey = (model: ModelChoice) => `${model.providerID}/${model.modelID}`;

export function modelPayload(value: string, models: ModelChoice[]) {
  const selected = models.find((model) => modelKey(model) === value);
  return selected ? { model: { providerID: selected.providerID, modelID: selected.modelID } } : {};
}

export function useWorkers(offset = 0, limit = 50) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["workers", offset, limit],
    enabled: !!api,
    refetchInterval: 10_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/workers", { params: { query: { limit, offset } } }),
      )) as unknown as Page<Worker>,
  });
}

export function useWorker(name: string) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["worker", name],
    enabled: !!api && !!name,
    refetchInterval: 10_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/workers/{name}", { params: { path: { name } } }),
      )) as unknown as Worker,
  });
}

export function useWorkerSessions(name: string, offset: number) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["worker", name, "sessions", offset],
    enabled: !!api && !!name,
    refetchInterval: 5_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/workers/{name}/sessions", {
          params: { path: { name }, query: { limit: 50, offset } },
        }),
      )) as unknown as Page<Session>,
  });
}

export function useRecentSessions(offset: number) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["sessions", "recent", offset],
    enabled: !!api,
    refetchInterval: 5_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/sessions", {
          params: { query: { ordering: "recent", limit: 20, offset } },
        }),
      )) as unknown as Page<Session>,
  });
}

export function useSessionDetail(worker: string, sid: string) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["session", worker, sid, "detail"],
    enabled: !!api && !!worker && !!sid,
    refetchInterval: 5_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/workers/{name}/sessions/{sid}", {
          params: { path: { name: worker, sid }, query: { compact: "true" } },
        }),
      )) as unknown as Session,
  });
}

export function useLatestMessages(worker: string, sid: string, live: boolean) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["session", worker, sid, "messages"],
    enabled: !!api && !!worker && !!sid,
    refetchInterval: live ? 15_000 : 5_000,
    queryFn: () => fetchMessages(api!, worker, sid),
  });
}

export async function fetchMessages(
  api: NonNullable<ReturnType<typeof useSession>["api"]>,
  worker: string,
  sid: string,
  before?: string,
) {
  return (await unwrap(
    api.GET("/api/v1/workers/{name}/sessions/{sid}/messages", {
      params: {
        path: { name: worker, sid },
        query: { latest: "true", limit: 10, ...(before ? { before } : {}) },
      },
    }),
  )) as unknown as MessageWindow;
}

export function useCommandStatus(id: string) {
  const { api } = useSession();
  return useQuery({
    queryKey: ["command", id],
    enabled: !!api && !!id,
    refetchInterval: 1_000,
    queryFn: async () =>
      (await unwrap(
        api!.GET("/api/v1/commands/{id}", { params: { path: { id } } }),
      )) as unknown as RemoteCommand,
  });
}

/** Queue a worker command; an unchanged retry reuses its idempotency key. */
export function useCommandQueue() {
  const { api } = useSession();
  const client = useQueryClient();
  const intents = useRef(new Map<string, string>());
  return useMutation({
    retry: false,
    mutationFn: async (input: CommandInput) => {
      const body = JSON.stringify(input);
      const key = intents.current.get(body) || requestId();
      intents.current.set(body, key);
      return (await unwrap(
        api!.POST("/api/v1/commands", {
          // Commands without a session (create) must omit the field entirely.
          body: {
            worker: input.worker,
            action: input.action,
            payload: input.payload,
            ...(input.sessionID ? { sessionID: input.sessionID } : {}),
            client_request_id: key,
          } as never,
        }),
      )) as unknown as RemoteCommand;
    },
    onSuccess: (_data, input) => {
      intents.current.delete(JSON.stringify(input));
      void client.invalidateQueries({ queryKey: ["worker"] });
      void client.invalidateQueries({ queryKey: ["session"] });
      void client.invalidateQueries({ queryKey: ["interactions"] });
    },
    onError: () => {
      void client.invalidateQueries({ queryKey: ["session"] });
    },
  });
}

export type CommandQueue = ReturnType<typeof useCommandQueue>;
