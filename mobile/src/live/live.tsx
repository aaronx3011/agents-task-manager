// One live connection to /ws/dashboard/ for the whole app (#55, #76).
//
// The device token never goes over the socket: each connection first trades it
// for a single-use ticket (POST /api/v1/realtime/ticket, 60s) and sends that as a
// WebSocket subprotocol. `changed` notices invalidate cached queries, and the
// focused session (the chat on screen) is subscribed for `session.live`
// snapshots, mirroring frontend/src/live.ts and session-live.ts.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { AppState } from "react-native";
import { useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type { LiveSnapshot } from "@/api/types";
import { useSession } from "@/auth/session";

export type LiveState = "connecting" | "live" | "offline";

export interface ChangeEvent {
  type: "changed";
  event_type?: string | null;
  subject?: string | null;
  task_id?: number | null;
}

type Focus = { worker: string; sessionID: string };

interface Live {
  state: LiveState;
  /** Follow one session's live snapshots; returns the unsubscribe function. */
  focus: (target: Focus, listener: (snapshot: LiveSnapshot | null) => void) => () => void;
}

const LiveContext = createContext<Live>({ state: "offline", focus: () => () => undefined });

const PING_MS = 25_000;
const MAX_BACKOFF_MS = 30_000;
const URGENT = /^(interaction\.|permission\.|question\.|command\.)/;

function socketUrl(server: string) {
  return server.replace(/^http/, "ws").replace(/\/+$/, "") + "/ws/dashboard/";
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const { api, server } = useSession();
  const client = useQueryClient();
  const [state, setState] = useState<LiveState>("offline");
  const focused = useRef<{ target: Focus; listener: (s: LiveSnapshot | null) => void } | null>(
    null,
  );
  const send = useRef<(message: object) => void>(() => undefined);
  const resubscribe = useRef<() => void>(() => undefined);

  useEffect(() => {
    if (!api) return;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let ping: ReturnType<typeof setInterval> | null = null;
    let refresh: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let stopped = false;

    const subscribe = () => {
      const target = focused.current?.target;
      send.current({
        type: "subscribe",
        worker: target?.worker ?? null,
        sessionID: target?.sessionID ?? null,
      });
    };
    const clear = () => {
      if (retry) clearTimeout(retry);
      if (ping) clearInterval(ping);
      retry = ping = null;
    };
    const schedule = () => {
      if (stopped) return;
      const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempts) * (0.5 + Math.random());
      attempts += 1;
      retry = setTimeout(connect, delay);
    };
    const changed = (event: ChangeEvent) => {
      const urgent = URGENT.test(event.event_type || "") || event.event_type === "session.error";
      if (event.subject && !urgent && ["workers", "sessions", "messages"].includes(event.subject))
        return;
      // Coalesce bursts of agent events into one refresh of what is on screen.
      if (refresh) clearTimeout(refresh);
      refresh = setTimeout(() => void client.invalidateQueries(), 250);
    };
    async function connect() {
      clear();
      if (stopped) return;
      setState("connecting");
      let subprotocols: string[];
      try {
        subprotocols = (await unwrap(api!.POST("/api/v1/realtime/ticket"))).subprotocols;
      } catch {
        setState("offline");
        schedule();
        return;
      }
      if (stopped) return;
      const current = new WebSocket(socketUrl(server), subprotocols);
      socket = current;
      send.current = (message) => {
        if (current.readyState === WebSocket.OPEN) current.send(JSON.stringify(message));
      };
      current.onopen = () => {
        attempts = 0;
        ping = setInterval(() => send.current({ type: "ping" }), PING_MS);
      };
      current.onmessage = (message) => {
        let data: { type?: string } & Record<string, unknown>;
        try {
          data = JSON.parse(String(message.data));
        } catch {
          return;
        }
        if (data.type === "connected") {
          setState("live");
          subscribe();
          void client.invalidateQueries();
        } else if (data.type === "changed") changed(data as unknown as ChangeEvent);
        else if (data.type === "session.live") {
          const snapshot = data as unknown as LiveSnapshot;
          const target = focused.current;
          if (
            target &&
            target.target.worker === snapshot.worker &&
            target.target.sessionID === snapshot.sessionID
          )
            target.listener(snapshot);
        } else if (data.type === "session.gap") {
          const target = focused.current;
          if (target) {
            target.listener(null);
            void client.invalidateQueries({
              queryKey: ["session", target.target.worker, target.target.sessionID],
            });
          }
        }
      };
      current.onclose = () => {
        if (socket !== current) return;
        socket = null;
        send.current = () => undefined;
        clear();
        setState("offline");
        schedule();
      };
    }
    const close = () => {
      clear();
      const current = socket;
      socket = null;
      send.current = () => undefined;
      current?.close();
    };
    resubscribe.current = subscribe;
    void connect();
    const subscription = AppState.addEventListener("change", (status) => {
      if (status === "active") {
        attempts = 0;
        if (!socket) void connect();
      } else {
        close();
        setState("offline");
      }
    });
    return () => {
      stopped = true;
      if (refresh) clearTimeout(refresh);
      subscription.remove();
      close();
    };
  }, [api, server, client]);

  const focus = useCallback<Live["focus"]>((target, listener) => {
    const entry = { target, listener };
    focused.current = entry;
    resubscribe.current();
    return () => {
      if (focused.current === entry) {
        focused.current = null;
        resubscribe.current();
      }
    };
  }, []);
  // Signed out there is no socket, whatever the last connection did.
  const current = api ? state : "offline";
  const value = useMemo<Live>(() => ({ state: current, focus }), [current, focus]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLiveState() {
  return useContext(LiveContext).state;
}

/** The latest live snapshot of the session on screen, or null between bursts. */
export function useFocusedSession(worker: string, sessionID: string) {
  const { focus } = useContext(LiveContext);
  const client = useQueryClient();
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  useEffect(() => {
    if (!worker || !sessionID) return;
    let expiry: ReturnType<typeof setTimeout> | undefined;
    const stop = focus({ worker, sessionID }, (next) => {
      clearTimeout(expiry);
      if (!next) {
        setSnapshot(null);
        return;
      }
      setSnapshot((previous) =>
        previous?.epoch === next.epoch && previous.sequence >= next.sequence ? previous : next,
      );
      // Snapshots stop once the session goes idle; fall back to stored history.
      expiry = setTimeout(() => {
        setSnapshot(null);
        void client.invalidateQueries({ queryKey: ["session", worker, sessionID] });
      }, 15_000);
    });
    return () => {
      clearTimeout(expiry);
      stop();
      setSnapshot(null);
    };
  }, [client, focus, worker, sessionID]);
  return snapshot;
}
