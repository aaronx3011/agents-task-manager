import { useCallback, useEffect, useState } from "react";

export interface SessionReference {
  worker: string;
  sessionID: string;
  title: string;
}
export interface SavedSession extends SessionReference {
  pinned: boolean;
  openedAt: number;
}
export const sessionIdentity = (
  session: Pick<SessionReference, "worker" | "sessionID">,
) => JSON.stringify([session.worker, session.sessionID]);

export function rememberSession(
  entries: SavedSession[],
  session: SessionReference,
  now: number,
  pin = false,
): SavedSession[] {
  const previous = entries.find(
    (entry) => sessionIdentity(entry) === sessionIdentity(session),
  );
  // Concurrent panel detail loads must not reorder other panels underneath a
  // user's pointer or while they are typing. Only explicit pin actions reorder.
  if (previous?.pinned && !pin)
    return entries.map((entry) =>
      sessionIdentity(entry) === sessionIdentity(session)
        ? { ...entry, ...session, openedAt: now }
        : entry,
    );
  const next = [
    {
      ...session,
      openedAt: now,
      pinned: pin ? !previous?.pinned : previous?.pinned || false,
    },
    ...entries.filter(
      (entry) => sessionIdentity(entry) !== sessionIdentity(session),
    ),
  ];
  return [
    ...next.filter((entry) => entry.pinned).slice(0, 100),
    ...next.filter((entry) => !entry.pinned).slice(0, 50),
  ];
}

export function useSessionLibrary(username: string) {
  // Saved sessions are per user on a shared browser.
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
  const [entries, setEntries] = useState<SavedSession[]>(() => {
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
            typeof entry.pinned === "boolean" &&
            Number.isFinite(entry.openedAt),
        )
        .slice(0, 150);
    } catch {
      return [];
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(entries));
    } catch {
      /* Browser storage may be disabled. */
    }
  }, [entries, storageKey]);
  const remember = useCallback((session: SessionReference) => {
    setEntries((previous) => rememberSession(previous, session, Date.now()));
  }, []);
  const togglePin = useCallback((session: SessionReference) => {
    setEntries((previous) =>
      rememberSession(previous, session, Date.now(), true),
    );
  }, []);
  return { entries, remember, togglePin, layout, setLayout };
}
export type SessionLibrary = ReturnType<typeof useSessionLibrary>;
