// Pinned and recently opened sessions, kept on this device per user, like the
// web's session library (frontend/src/session-library.ts).
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
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
  pin = false,
): SavedSession[] {
  const previous = entries.find((entry) => sessionIdentity(entry) === sessionIdentity(session));
  const next = [
    {
      ...session,
      openedAt: now,
      pinned: pin ? !previous?.pinned : previous?.pinned || false,
    },
    ...entries.filter((entry) => sessionIdentity(entry) !== sessionIdentity(session)),
  ];
  return [
    ...next.filter((entry) => entry.pinned).slice(0, 100),
    ...next.filter((entry) => !entry.pinned).slice(0, 50),
  ];
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
  const { username } = useAccess();
  const key = `atm.sessions:${username}`;
  const [loaded, setLoaded] = useState<{ key: string; entries: SavedSession[] } | null>(null);
  useEffect(() => {
    let active = true;
    void AsyncStorage.getItem(key)
      .then((raw) => {
        const parsed: unknown = JSON.parse(raw || "[]");
        return Array.isArray(parsed) ? parsed.filter(valid).slice(0, 150) : [];
      })
      .catch(() => [] as SavedSession[])
      .then((entries) => {
        if (active) setLoaded({ key, entries });
      });
    return () => {
      active = false;
    };
  }, [key]);
  const entries = useMemo(() => (loaded?.key === key ? loaded.entries : []), [loaded, key]);
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
    (session: SessionReference) =>
      update((previous) => rememberSession(previous, session, Date.now(), true)),
    [update],
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
