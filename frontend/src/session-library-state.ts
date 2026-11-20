export interface SessionReference {
  worker: string;
  sessionID: string;
  title: string;
}
export interface SavedSession extends SessionReference {
  pinned: boolean;
  openedAt: number;
}
export interface SessionPin extends SessionReference {
  pinned_at: string;
}
export const sessionIdentity = (
  session: Pick<SessionReference, "worker" | "sessionID">,
) => JSON.stringify([session.worker, session.sessionID]);

/** Device-local navigation history. Pins come only from the server. */
export function rememberSession(
  entries: SavedSession[],
  session: SessionReference,
  now: number,
): SavedSession[] {
  return [
    { ...session, openedAt: now, pinned: false },
    ...entries.filter(
      (entry) => sessionIdentity(entry) !== sessionIdentity(session),
    ),
  ].slice(0, 50);
}

/** Server pins are authoritative and ordered; recents fill the remainder. */
export function mergeSessionPins(
  recent: SavedSession[],
  pins: SessionPin[],
): SavedSession[] {
  const identities = new Set(pins.map(sessionIdentity));
  return [
    ...pins.map((pin) => ({
      worker: pin.worker,
      sessionID: pin.sessionID,
      title: pin.title,
      pinned: true,
      openedAt: Date.parse(pin.pinned_at),
    })),
    ...recent
      .filter((entry) => !identities.has(sessionIdentity(entry)))
      .map((entry) => ({ ...entry, pinned: false })),
  ];
}
