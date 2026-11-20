import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "./api-client";
import { useAccess } from "./auth";
import {
  mergeSessionPins,
  rememberSession,
  sessionIdentity,
} from "./session-library-state";
import type { SavedSession, SessionReference } from "./session-library-state";
export { sessionIdentity } from "./session-library-state";
export type { SessionReference } from "./session-library-state";

export function useSessionLibrary(username: string) {
  const { canWrite } = useAccess();
  const client = useQueryClient();
  const pins = useQuery({
    queryKey: ["session-pins"],
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/session-pins", { signal })),
    refetchInterval: 5000,
  });
  const mutation = useMutation({
    mutationFn: (body: {
      worker: string;
      sessionID: string;
      pinned: boolean;
    }) => unwrap(api.POST("/api/v1/session-pins", { body })),
    onSettled: () => client.invalidateQueries({ queryKey: ["session-pins"] }),
  });
  // Recent navigation and panel layout stay device-local; pins never do.
  const storageKey = `agent-ops-sessions:${username}`;
  const preferencesKey = `${storageKey}:layout`;
  const [layout, setLayout] = useState(() => {
    try {
      const value = JSON.parse(localStorage.getItem(preferencesKey) || "{}");
      return {
        parallel: value.parallel === true,
        maximum: Math.max(
          1,
          Math.min(6, Math.trunc(Number(value.maximum)) || 3),
        ),
      };
    } catch {
      return { parallel: false, maximum: 3 };
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(preferencesKey, JSON.stringify(layout));
    } catch {
      /* Storage may be disabled. */
    }
  }, [layout, preferencesKey]);
  const [recent, setRecent] = useState<SavedSession[]>(() => {
    try {
      const stored: unknown = JSON.parse(
        localStorage.getItem(storageKey) || "[]",
      );
      if (!Array.isArray(stored)) return [];
      return stored
        .filter(
          (entry): entry is SavedSession =>
            entry &&
            typeof entry.worker === "string" &&
            typeof entry.sessionID === "string" &&
            typeof entry.title === "string" &&
            Number.isFinite(entry.openedAt),
        )
        .map((entry) => ({ ...entry, pinned: false }))
        .slice(0, 50);
    } catch {
      return [];
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(recent));
    } catch {
      /* Browser storage may be disabled. */
    }
  }, [recent, storageKey]);
  const remember = useCallback((session: SessionReference) => {
    setRecent((previous) => rememberSession(previous, session, Date.now()));
  }, []);
  const entries = useMemo(
    () => mergeSessionPins(recent, pins.data?.items || []),
    [recent, pins.data],
  );
  const togglePin = useCallback(
    (session: SessionReference) => {
      // Repeated set operations are idempotent server-side, so bulk pinning
      // may dispatch several at once without dropping later items.
      if (!canWrite) return;
      mutation.mutate({
        worker: session.worker,
        sessionID: session.sessionID,
        pinned: !entries.some(
          (entry) =>
            entry.pinned && sessionIdentity(entry) === sessionIdentity(session),
        ),
      });
    },
    [canWrite, mutation, entries],
  );
  const retry = useCallback(() => {
    mutation.reset();
    void pins.refetch();
  }, [mutation, pins]);
  return {
    entries,
    remember,
    togglePin,
    layout,
    setLayout,
    error: mutation.error || pins.error,
    pending: pins.isPending || mutation.isPending,
    canPin: canWrite,
    retry,
  };
}
export type SessionLibrary = ReturnType<typeof useSessionLibrary>;
