import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertCircle,
  ArrowUp,
  Check,
  Circle,
  Link2,
  LoaderCircle,
  Minus,
} from "lucide-react";
import { LABELS } from "./types";
import type { Status, Task } from "./types";

const Clock = createContext(Date.now());
export function useNow() {
  return useContext(Clock);
}
export function ClockProvider({
  serverTime,
  children,
}: {
  serverTime?: string;
  children: ReactNode;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const offset = serverTime ? Date.parse(serverTime) - Date.now() : 0;
    setNow(Date.now() + offset);
    const timer = setInterval(() => setNow(Date.now() + offset), 1000);
    return () => clearInterval(timer);
  }, [serverTime]);
  return <Clock.Provider value={now}>{children}</Clock.Provider>;
}
export function Age({
  value,
  heartbeat = false,
}: {
  value: string | null;
  heartbeat?: boolean;
}) {
  const now = useContext(Clock);
  if (!value) return <span className="muted">Not seen</span>;
  const seconds = Math.max(0, Math.floor((now - Date.parse(value)) / 1000));
  const label =
    seconds < 5
      ? "just now"
      : seconds < 60
        ? `${seconds}s ago`
        : seconds < 3600
          ? `${Math.floor(seconds / 60)}m ago`
          : seconds < 86400
            ? `${Math.floor(seconds / 3600)}h ago`
            : `${Math.floor(seconds / 86400)}d ago`;
  return (
    <time
      className={heartbeat && seconds > 900 ? "overdue" : ""}
      dateTime={value}
      title={`${new Date(value).toLocaleString()}${heartbeat && seconds > 900 ? " · Heartbeat overdue" : ""}`}
    >
      {label}
    </time>
  );
}
const colors = ["mint", "purple", "blue", "peach", "rose"];
export function tone(value: string) {
  return colors[
    [...value].reduce((sum, char) => sum + char.charCodeAt(0), 0) %
      colors.length
  ];
}
export function Avatar({
  name,
  small = false,
}: {
  name: string;
  small?: boolean;
}) {
  return (
    <span
      className={`avatar ${tone(name)} ${small ? "small" : ""}`}
      aria-hidden="true"
    >
      {name
        .split(/[\s_-]/)
        .filter(Boolean)
        .map((part) => part[0])
        .join("")
        .slice(0, 2)
        .toUpperCase()}
    </span>
  );
}
export function StatusBadge({ status }: { status: Status }) {
  return (
    <span className={`status-badge status-${status}`}>
      <span className="status-dot" />
      {LABELS[status]}
    </span>
  );
}
export function Priority({ value }: { value: number }) {
  const label =
    value <= 0
      ? "Urgent"
      : value <= 25
        ? "High"
        : value <= 100
          ? "Normal"
          : "Low";
  const Icon = value <= 25 ? ArrowUp : Minus;
  return (
    <span
      className={`priority priority-${label.toLowerCase()}`}
      title={`Priority ${value} · lower values are claimed first`}
    >
      <Icon size={12} />
      {label}
    </span>
  );
}
export function DependencyBadge({ task }: { task: Task }) {
  return task.dependency_progress.total > 0 ? (
    <span
      className="dependency-badge"
      title="Completed prerequisites / total prerequisites"
    >
      <Link2 size={12} />
      {task.dependency_progress.done}/{task.dependency_progress.total}
    </span>
  ) : null;
}
export function Empty({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-symbol">
        <Circle size={22} />
      </span>
      <strong>{title}</strong>
      {description && <p>{description}</p>}
      {children}
    </div>
  );
}
export function ErrorState({
  error,
  retry,
}: {
  error: Error;
  retry?: () => void;
}) {
  return (
    <div className="error-state" role="alert">
      <AlertCircle size={18} />
      <div>
        <strong>We couldn’t load this view</strong>
        <p>{error.message}</p>
      </div>
      {retry && (
        <button className="button secondary" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}
export function Loading({ label = "Loading workspace" }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <LoaderCircle size={19} className="spin" />
      {label}
    </div>
  );
}
export function ReadyMark({ task }: { task: Task }) {
  return task.status === "pending" ? (
    <span className={`ready-mark ${task.is_ready ? "ready" : "waiting"}`}>
      {task.is_ready ? <Check size={11} /> : <Link2 size={11} />}
      {task.is_ready ? "Ready" : "Waiting"}
    </span>
  ) : null;
}
