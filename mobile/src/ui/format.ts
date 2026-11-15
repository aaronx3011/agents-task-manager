export function age(value: string | null | undefined, now = Date.now()) {
  if (!value) return "Not seen";
  const seconds = Math.max(0, Math.floor((now - Date.parse(value)) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/** A past or future time, relative to now: "in 3d" or "2h ago". */
export function relative(value: string | null | undefined, now = Date.now()) {
  if (!value) return "—";
  const future = Date.parse(value) > now;
  const span = Math.abs(Date.parse(value) - now) / 1000;
  const text =
    span < 60
      ? `${Math.round(span)}s`
      : span < 3600
        ? `${Math.round(span / 60)}m`
        : span < 86400
          ? `${Math.round(span / 3600)}h`
          : `${Math.round(span / 86400)}d`;
  return future ? `in ${text}` : `${text} ago`;
}

export function overdue(value: string | null | undefined, now = Date.now()) {
  return !!value && now - Date.parse(value) > 900_000;
}

export function dateTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : "—";
}

export function words(value: string) {
  return value.replaceAll("_", " ").replaceAll(".", " ");
}

export function errorText(error: unknown, fallback = "Something went wrong") {
  return error instanceof Error && error.message ? error.message : fallback;
}

let sequence = 0;
/** Idempotency key for commands, so a retried tap never queues twice. */
export function requestId(prefix = "mobile") {
  sequence += 1;
  return `${prefix}-${Date.now().toString(36)}-${sequence}-${Math.random().toString(36).slice(2, 8)}`;
}
