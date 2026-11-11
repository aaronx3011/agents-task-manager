// Live updates from /ws/dashboard/ while the app is in the foreground (#55).
//
// The device token never goes over the socket: each connection first trades it
// for a single-use ticket (POST /api/v1/realtime/ticket, 60s) and sends that as a
// WebSocket subprotocol. Reconnects use exponential backoff with jitter, and the
// socket closes when the app goes to the background.
import { useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { unwrap } from "@/api/client";
import { useSession } from "@/auth/session";

export type LiveState = "connecting" | "live" | "offline";

export interface ChangeEvent {
  type: "changed";
  event_type?: string | null;
  subject?: string | null;
  task_id?: number | null;
}

const PING_MS = 25_000;
const MAX_BACKOFF_MS = 30_000;

function socketUrl(server: string) {
  return server.replace(/^http/, "ws").replace(/\/+$/, "") + "/ws/dashboard/";
}

export function useLive(onChange: (event: ChangeEvent) => void): LiveState {
  const { api, server } = useSession();
  const [state, setState] = useState<LiveState>("connecting");
  const handler = useRef(onChange);
  useEffect(() => {
    handler.current = onChange;
  }, [onChange]);

  useEffect(() => {
    if (!api) return;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let ping: ReturnType<typeof setInterval> | null = null;
    let attempts = 0;
    let stopped = false;

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
    async function connect() {
      clear();
      if (stopped) return;
      setState("connecting");
      let subprotocols: string[];
      try {
        const ticket = await unwrap(api!.POST("/api/v1/realtime/ticket"));
        subprotocols = ticket.subprotocols;
      } catch {
        setState("offline");
        schedule();
        return;
      }
      if (stopped) return;
      const current = new WebSocket(socketUrl(server), subprotocols);
      socket = current;
      current.onopen = () => {
        attempts = 0;
        setState("live");
        ping = setInterval(() => current.send(JSON.stringify({ type: "ping" })), PING_MS);
      };
      current.onmessage = (message) => {
        try {
          const data = JSON.parse(String(message.data));
          if (data?.type === "changed") handler.current(data as ChangeEvent);
        } catch {
          // Ignore frames that are not JSON.
        }
      };
      current.onclose = () => {
        if (socket !== current) return;
        socket = null;
        clear();
        setState("offline");
        schedule();
      };
    }

    const close = () => {
      clear();
      const current = socket;
      socket = null;
      current?.close();
    };
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
      subscription.remove();
      close();
    };
  }, [api, server]);

  return state;
}
