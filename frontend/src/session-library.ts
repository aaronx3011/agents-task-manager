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

export function useSessionLibrary() {
  const username = document.getElementById("root")?.dataset.username || "staff";
  const storageKey = `agent-ops-sessions:${username}`;
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
  return { entries, remember, togglePin };
}
export type SessionLibrary = ReturnType<typeof useSessionLibrary>;
