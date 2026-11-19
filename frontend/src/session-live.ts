import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Message } from "./opencode-types";

type Focus = { worker: string; sessionID: string };
export interface LiveSnapshot extends Focus {
  type: "session.live";
  epoch: string;
  sequence: number;
  executionStatus?: string;
  messages: Pick<Message, "remoteID" | "role" | "content" | "parts">[];
}
const focuses = new Map<object, Focus>();
const focusListeners = new Set<() => void>();
const snapshotListeners = new Set<
  (snapshot: LiveSnapshot | null, target?: Focus) => void
>();
export const getFocus = () => (document.hidden ? [] : [...focuses.values()]);
export function watchFocus(listener: () => void) {
  focusListeners.add(listener);
  return () => {
    focusListeners.delete(listener);
  };
}
export function publishSnapshot(snapshot: LiveSnapshot | null, target?: Focus) {
  snapshotListeners.forEach((listener) => listener(snapshot, target));
}

export function useFocusedSession(worker: string, sessionID: string) {
  const client = useQueryClient();
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  useEffect(() => {
    const selected = { worker, sessionID };
    let expiry: ReturnType<typeof setTimeout>;
    let update: ReturnType<typeof setTimeout> | undefined;
    let pending: LiveSnapshot | null = null;
    function select() {
      focuses.set(selected, selected);
      focusListeners.forEach((listener) => listener());
      if (!document.hidden) {
        void client.invalidateQueries({
          queryKey: ["opencodes", worker, sessionID],
        });
      } else {
        clearTimeout(update);
        update = undefined;
        pending = null;
        setSnapshot(null);
      }
    }
    const receive = (next: LiveSnapshot | null, target?: Focus) => {
      if (!next) {
        if (
          target &&
          (target.worker !== worker || target.sessionID !== sessionID)
        )
          return;
        clearTimeout(update);
        update = undefined;
        pending = null;
        setSnapshot(null);
        return;
      }
      if (
        document.hidden ||
        next.worker !== worker ||
        next.sessionID !== sessionID
      )
        return;
      if (pending?.epoch === next.epoch && pending.sequence >= next.sequence)
        return;
      pending = next;
      // One replaceable pending frame per session: a noisy conversation cannot
      // enqueue an unbounded React render backlog or starve another panel.
      if (!update)
        update = setTimeout(() => {
          const latest = pending;
          pending = null;
          update = undefined;
          if (latest)
            setSnapshot((previous) =>
              previous?.epoch === latest.epoch &&
              previous.sequence >= latest.sequence
                ? previous
                : latest,
            );
        }, 100);
      clearTimeout(expiry);
      expiry = setTimeout(() => {
        setSnapshot(null);
        void client.invalidateQueries({
          queryKey: ["opencodes", worker, sessionID],
        });
      }, 15000);
    };
    snapshotListeners.add(receive);
    select();
    document.addEventListener("visibilitychange", select);
    return () => {
      clearTimeout(expiry);
      clearTimeout(update);
      snapshotListeners.delete(receive);
      document.removeEventListener("visibilitychange", select);
      focuses.delete(selected);
      focusListeners.forEach((listener) => listener());
    };
  }, [client, worker, sessionID]);
  return snapshot?.worker === worker && snapshot.sessionID === sessionID
    ? snapshot
    : null;
}

export function mergeLiveMessages(
  saved: Message[],
  snapshot: LiveSnapshot | null,
  maximum?: number,
): Message[] {
  const messages = new Map(saved.map((message) => [message.remoteID, message]));
  for (const message of snapshot?.messages || []) {
    const previous = messages.get(message.remoteID);
    // An older sampled partial must not replace a durably completed message.
    const savedTime = previous?.content.time as
      { completed?: number } | undefined;
    const liveTime = message.content.time as { completed?: number } | undefined;
    if (savedTime?.completed && !liveTime?.completed) continue;
    messages.set(message.remoteID, {
      ...message,
      id: previous?.id ?? 0,
      publishedAt:
        previous?.publishedAt ??
        new Date(
          Number(
            (message.content.time as { created?: number } | undefined)?.created,
          ) || Date.now(),
        ).toISOString(),
    });
  }
  const ordered = [...messages.values()].sort((a, b) => {
    const first = (a.content.time as { created?: number } | undefined)?.created;
    const second = (b.content.time as { created?: number } | undefined)
      ?.created;
    return first && second ? first - second : 0;
  });
  return maximum ? ordered.slice(-maximum) : ordered;
}
