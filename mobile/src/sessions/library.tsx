// Shared server pins plus device-local recent navigation.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { Alert } from "react-native";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import { useSession } from "@/auth/session";
import { useAccess } from "@/auth/access";

export interface SessionReference {
  worker: string;
  sessionID: string;
  title: string;
}
export interface SavedSession extends SessionReference {
  pinned: boolean;
  openedAt: number;
}

export const sessionIdentity = (session: Pick<SessionReference, "worker" | "sessionID">) =>
  JSON.stringify([session.worker, session.sessionID]);

export function rememberSession(
  entries: SavedSession[],
  session: SessionReference,
  now: number,
): SavedSession[] {
  const next = [
    {
      ...session,
      openedAt: now,
      pinned: false,
    },
    ...entries.filter((entry) => sessionIdentity(entry) !== sessionIdentity(session)),
  ];
  return next.slice(0, 50);
}

function valid(entry: unknown): entry is SavedSession {
  const value = entry as SavedSession;
  return (
    !!value &&
    typeof value.worker === "string" &&
    typeof value.sessionID === "string" &&
    typeof value.title === "string" &&
    typeof value.pinned === "boolean" &&
    Number.isFinite(value.openedAt)
  );
}

interface Library {
  entries: SavedSession[];
  remember: (session: SessionReference) => void;
  togglePin: (session: SessionReference) => void;
  isPinned: (session: Pick<SessionReference, "worker" | "sessionID">) => boolean;
}

const LibraryContext = createContext<Library | null>(null);

export function SessionLibraryProvider({ children }: { children: ReactNode }) {
  const { username, canWrite } = useAccess();
  const { api } = useSession();
  const client = useQueryClient();
  const pins = useQuery({
    queryKey: ["session-pins"],
    enabled: !!api,
    queryFn: ({ signal }) => unwrap(api!.GET("/api/v1/session-pins", { signal })),
    refetchInterval: 5000,
  });
  const mutation = useMutation({
    mutationFn: (body: { worker: string; sessionID: string; pinned: boolean }) =>
      unwrap(api!.POST("/api/v1/session-pins", { body })),
    onSettled: () => client.invalidateQueries({ queryKey: ["session-pins"] }),
    onError: (error) => Alert.alert("Could not update shared pin", error.message),
  });
  const key = `atm.sessions:${username}`;
  const [loaded, setLoaded] = useState<{ key: string; entries: SavedSession[] } | null>(null);
  useEffect(() => {
    let active = true;
    void AsyncStorage.getItem(key)
      .then((raw) => {
        const parsed: unknown = JSON.parse(raw || "[]");
        return Array.isArray(parsed)
          ? parsed
              .filter(valid)
              .map((entry) => ({ ...entry, pinned: false }))
              .slice(0, 50)
          : [];
      })
      .catch(() => [] as SavedSession[])
      .then((entries) => {
        if (active) setLoaded({ key, entries });
      });
    return () => {
      active = false;
    };
  }, [key]);
  const recent = useMemo(() => (loaded?.key === key ? loaded.entries : []), [loaded, key]);
  const entries = useMemo(() => {
    const shared = pins.data?.items || [];
    const identities = new Set(shared.map(sessionIdentity));
    return [
      ...shared.map((pin) => ({ ...pin, pinned: true, openedAt: Date.parse(pin.pinned_at) })),
      ...recent.filter((entry) => !identities.has(sessionIdentity(entry))),
    ];
  }, [recent, pins.data]);
  const update = useCallback(
    (change: (entries: SavedSession[]) => SavedSession[]) =>
      setLoaded((current) => {
        const next = change(current?.key === key ? current.entries : []);
        void AsyncStorage.setItem(key, JSON.stringify(next)).catch(() => undefined);
        return { key, entries: next };
      }),
    [key],
  );
  const remember = useCallback(
    (session: SessionReference) =>
      update((previous) => rememberSession(previous, session, Date.now())),
    [update],
  );
  const togglePin = useCallback(
    (session: SessionReference) => {
      if (!canWrite || !api || pins.isPending || mutation.isPending) return;
      mutation.mutate({
        worker: session.worker,
        sessionID: session.sessionID,
        pinned: !entries.some(
          (entry) => entry.pinned && sessionIdentity(entry) === sessionIdentity(session),
        ),
      });
    },
    [canWrite, api, pins.isPending, mutation, entries],
  );
  const value = useMemo<Library>(
    () => ({
      entries,
      remember,
      togglePin,
      isPinned: (session) =>
        entries.some(
          (entry) => entry.pinned && sessionIdentity(entry) === sessionIdentity(session),
        ),
    }),
    [entries, remember, togglePin],
  );
  return <LibraryContext.Provider value={value}>{children}</LibraryContext.Provider>;
}

export function useSessionLibrary() {
  const value = useContext(LibraryContext);
  if (!value) throw new Error("useSessionLibrary outside SessionLibraryProvider");
  return value;
}
