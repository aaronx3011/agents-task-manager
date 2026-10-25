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
let focus: Focus | null = null;
const focusListeners = new Set<() => void>();
const snapshotListeners = new Set<(snapshot: LiveSnapshot | null) => void>();
export const getFocus = () => focus;
export function watchFocus(listener: () => void) {
  focusListeners.add(listener);
  return () => {
    focusListeners.delete(listener);
  };
}
export function publishSnapshot(snapshot: LiveSnapshot | null) {
  snapshotListeners.forEach((listener) => listener(snapshot));
}

export function useFocusedSession(worker: string, sessionID: string) {
  const client = useQueryClient();
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  useEffect(() => {
    const selected = { worker, sessionID };
    let expiry: ReturnType<typeof setTimeout>;
    function select() {
      focus = document.hidden ? null : selected;
      focusListeners.forEach((listener) => listener());
      if (!document.hidden) {
        void client.invalidateQueries({
          queryKey: ["opencodes", worker, sessionID],
        });
      } else setSnapshot(null);
    }
    const receive = (next: LiveSnapshot | null) => {
      if (!next) {
        setSnapshot(null);
        return;
      }
      if (
        document.hidden ||
        next.worker !== worker ||
        next.sessionID !== sessionID
      )
        return;
      setSnapshot((previous) =>
        previous?.epoch === next.epoch && previous.sequence >= next.sequence
          ? previous
          : next,
      );
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
      snapshotListeners.delete(receive);
      document.removeEventListener("visibilitychange", select);
      if (focus === selected) {
        focus = null;
        focusListeners.forEach((listener) => listener());
      }
    };
  }, [client, worker, sessionID]);
  return snapshot?.worker === worker && snapshot.sessionID === sessionID
    ? snapshot
    : null;
}

export function mergeLiveMessages(
  saved: Message[],
  snapshot: LiveSnapshot | null,
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
  return [...messages.values()].sort((a, b) => {
    const first = (a.content.time as { created?: number } | undefined)?.created;
    const second = (b.content.time as { created?: number } | undefined)
      ?.created;
    return first && second ? first - second : 0;
  });
}
