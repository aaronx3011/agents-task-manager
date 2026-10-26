import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getFocus, publishSnapshot, watchFocus } from "./session-live";
import type { LiveSnapshot } from "./session-live";

export type Connection = "connecting" | "live" | "reconnecting" | "expired";
export function useLiveUpdates() {
  const client = useQueryClient();
  const [connection, setConnection] = useState<Connection>("connecting");
  const [highlighted, setHighlighted] = useState<Set<number>>(() => new Set());
  useEffect(() => {
    let disposed = false;
    let socket: WebSocket;
    let retry = 1000;
    let retryTimer: ReturnType<typeof setTimeout>;
    let updateTimer: ReturnType<typeof setTimeout> | undefined;
    let broad = false;
    const ids = new Set<number>();
    const highlights = new Map<number, ReturnType<typeof setTimeout>>();
    function subscribe() {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({
            type: "subscribe",
            worker: null,
            sessionID: null,
            ...getFocus(),
          }),
        );
      }
    }
    const unwatch = watchFocus(subscribe);
    function refresh() {
      void client.invalidateQueries({ queryKey: ["planning"] });
      void client.invalidateQueries({ queryKey: ["configuration", "agents"] });
      void client.invalidateQueries({ queryKey: ["opencodes"] });
      void client.invalidateQueries({ queryKey: ["workspace"] });
      void client.invalidateQueries({ queryKey: ["tasks"] });
      // Detail embeds prerequisites: their events can change a different open task.
      void client.invalidateQueries({ queryKey: ["task"] });
      if (broad) {
        void client.invalidateQueries({ queryKey: ["events"] });
      } else
        for (const id of ids) {
          void client.invalidateQueries({ queryKey: ["events", id] });
        }
      ids.clear();
      broad = false;
      updateTimer = undefined;
    }
    function connect() {
      if (disposed) return;
      socket = new WebSocket(
        `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws/dashboard/`,
      );
      socket.onmessage = (event) => {
        if (disposed) return;
        const message = JSON.parse(event.data) as {
          type: string;
          task_id?: number;
          event_type?: string;
          subject?: string;
        };
        if (message.type === "connected") {
          retry = 1000;
          setConnection("live");
          broad = true;
          refresh();
          subscribe();
        } else if (message.type === "session.live") {
          publishSnapshot(JSON.parse(event.data) as LiveSnapshot);
        } else if (message.type === "session.gap") {
          publishSnapshot(null);
          const focus = getFocus();
          if (focus)
            void client.invalidateQueries({
              queryKey: ["opencodes", focus.worker, focus.sessionID],
            });
        } else if (message.type === "changed") {
          // Ordinary worker snapshots do not refetch every conversation. The
          // overview polls; commands/interactions/failures still invalidate now.
          const urgent =
            /^(interaction\.|permission\.|question\.|command\.)/.test(
              message.event_type || "",
            ) || message.event_type === "session.error";
          if (
            message.subject &&
            !urgent &&
            ["workers", "sessions", "messages"].includes(message.subject)
          )
            return;
          if (
            message.task_id &&
            ["claimed", "released", "status_changed"].includes(
              message.event_type || "",
            )
          ) {
            const id = message.task_id;
            setHighlighted((previous) => new Set([...previous, id]));
            clearTimeout(highlights.get(id));
            highlights.set(
              id,
              setTimeout(() => {
                setHighlighted((previous) => {
                  const next = new Set(previous);
                  next.delete(id);
                  return next;
                });
                highlights.delete(id);
              }, 1800),
            );
          }
          if (message.task_id) ids.add(message.task_id);
          else broad = true;
          if (!updateTimer) updateTimer = setTimeout(refresh, 150);
        }
      };
      socket.onclose = (event) => {
        if (disposed) return;
        publishSnapshot(null);
        if (event.code === 4403) {
          setConnection("expired");
          return;
        }
        setConnection("reconnecting");
        retryTimer = setTimeout(connect, retry + Math.random() * 250);
        retry = Math.min(retry * 2, 15000);
      };
      socket.onerror = () => {
        if (!disposed) setConnection("reconnecting");
      };
    }
    connect();
    const heartbeat = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN)
        socket.send(JSON.stringify({ type: "ping" }));
    }, 25000);
    const recovery = setInterval(() => {
      broad = true;
      refresh();
    }, 60000);
    return () => {
      disposed = true;
      unwatch();
      clearTimeout(retryTimer);
      clearTimeout(updateTimer);
      clearInterval(heartbeat);
      clearInterval(recovery);
      highlights.forEach(clearTimeout);
      socket?.close();
    };
  }, [client]);
  return { connection, highlighted };
}
