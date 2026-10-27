import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Bell, ChevronRight, MessageSquare, X } from "lucide-react";
import { get } from "./api";
import type { Interaction, Session } from "./opencode-types";
import type { Page } from "./types";
import "./notifications.css";

type Navigate = (
  changes: Record<string, string | number | null>,
  push?: boolean,
) => void;

type PendingSession = Session & { pendingInteractions?: Interaction[] };

interface Alert {
  key: string;
  worker: string;
  sessionID: string;
  title: string;
  kind: Interaction["kind"];
  detail: string;
}

const ENDPOINT = "/api/sessions?pending=true&ordering=recent&limit=50";
const DISMISS_AFTER = 15000;
const MAX_VISIBLE = 3;

function detail(kind: Interaction["kind"], request?: Interaction) {
  if (kind === "permission")
    return (
      request?.title ||
      request?.type ||
      "A session is waiting for your approval."
    );
  const question = request?.questions?.[0];
  return question?.text || question?.header || "A session has a question.";
}

function alertsFrom(sessions: PendingSession[]): Alert[] {
  const alerts: Alert[] = [];
  for (const session of sessions) {
    const requests = session.pendingInteractions || [];
    if (!requests.length) continue;
    for (const request of requests) {
      alerts.push({
        key: `${session.worker}:${session.sessionID}:${request.kind}:${request.id}`,
        worker: session.worker,
        sessionID: session.sessionID,
        title: session.title || session.sessionID,
        kind: request.kind,
        detail: detail(request.kind, request),
      });
    }
  }
  return alerts;
}

export function ApprovalNotifications({ navigate }: { navigate: Navigate }) {
  const [open, setOpen] = useState(false);
  const [notices, setNotices] = useState<Alert[]>([]);
  const root = useRef<HTMLDivElement>(null);
  const seen = useRef<Set<string> | null>(null);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const sessions = useQuery({
    queryKey: ["approvals"],
    queryFn: ({ signal }) => get<Page<PendingSession>>(ENDPOINT, signal),
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
  const alerts = useMemo(
    () => alertsFrom(sessions.data?.items || []),
    [sessions.data],
  );

  useEffect(() => {
    // Wait for the first real response so existing approvals only raise the
    // badge; toasts are reserved for requests that appear while watching.
    if (!sessions.data) return;
    const keys = new Set(alerts.map((alert) => alert.key));
    if (seen.current === null) {
      seen.current = keys;
      return;
    }
    const fresh = alerts.filter((alert) => !seen.current?.has(alert.key));
    seen.current = keys;
    if (!fresh.length) return;
    setNotices((current) => [...fresh, ...current].slice(0, MAX_VISIBLE));
    for (const alert of fresh) {
      clearTimeout(timers.current.get(alert.key));
      timers.current.set(
        alert.key,
        setTimeout(() => dismiss(alert.key), DISMISS_AFTER),
      );
    }
  }, [alerts, sessions.data]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const timerMap = timers.current;
  useEffect(
    () => () => {
      timerMap.forEach(clearTimeout);
      timerMap.clear();
    },
    [timerMap],
  );

  function dismiss(key: string) {
    clearTimeout(timers.current.get(key));
    timers.current.delete(key);
    setNotices((current) => current.filter((alert) => alert.key !== key));
  }

  function visit(alert: Alert) {
    setOpen(false);
    dismiss(alert.key);
    navigate(
      {
        section: "sessions",
        worker: alert.worker,
        session: alert.sessionID,
        task: null,
      },
      true,
    );
  }

  const count = alerts.length;
  const label = count
    ? `${count} request${count === 1 ? "" : "s"} awaiting input`
    : "No approvals waiting";

  return (
    <div className="approval-notifications" ref={root}>
      <button
        className={`icon-button approval-bell ${count ? "has-pending" : ""}`}
        aria-label={`Notifications: ${label}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Bell size={17} />
        {count > 0 && (
          <span className="approval-count" aria-hidden="true">
            {count > 9 ? "9+" : count}
          </span>
        )}
      </button>
      {open && (
        <div
          className="approval-popover"
          role="dialog"
          aria-label="Sessions awaiting approval"
        >
          <header>
            <div>
              <strong>Approvals</strong>
              <span>{label}</span>
            </div>
            <button
              className="icon-button"
              aria-label="Close notifications"
              onClick={() => setOpen(false)}
            >
              <X size={15} />
            </button>
          </header>
          {count ? (
            <div className="approval-list">
              {alerts.map((alert) => (
                <button
                  className="approval-item"
                  key={alert.key}
                  onClick={() => visit(alert)}
                >
                  <span className={`approval-kind ${alert.kind}`}>
                    {alert.kind === "permission" ? "Approval" : "Question"}
                  </span>
                  <strong>{alert.title}</strong>
                  <p>{alert.detail}</p>
                  <small>
                    {alert.worker}
                    <ChevronRight size={12} />
                  </small>
                </button>
              ))}
            </div>
          ) : (
            <p className="approval-empty">
              <MessageSquare size={14} />
              Nothing needs your input right now.
            </p>
          )}
        </div>
      )}
      {notices.length > 0 && (
        <div className="approval-toasts" aria-live="assertive">
          {notices.map((alert) => (
            <div className="approval-toast" key={alert.key}>
              <button
                className="approval-toast-body"
                onClick={() => visit(alert)}
              >
                <span className={`approval-kind ${alert.kind}`}>
                  {alert.kind === "permission" ? "Approval" : "Question"}
                </span>
                <strong>{alert.title}</strong>
                <p>{alert.detail}</p>
                <small>
                  Open session
                  <ChevronRight size={12} />
                </small>
              </button>
              <button
                className="icon-button approval-toast-close"
                aria-label="Dismiss notification"
                onClick={() => dismiss(alert.key)}
              >
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
