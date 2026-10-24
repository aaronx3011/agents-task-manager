import type { Filters } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (
    response.redirected ||
    response.status === 401 ||
    response.status === 403
  ) {
    throw new ApiError(401, "Your session has expired. Sign in to reconnect.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      typeof body.detail === "string"
        ? body.detail
        : `Request failed (${response.status})`,
    );
  }
  return response.json() as Promise<T>;
}
export async function post<T>(path: string, body: unknown): Promise<T> {
  const token = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith("csrftoken="))
    ?.slice("csrftoken=".length);
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRFToken": token ? decodeURIComponent(token) : "",
    },
    body: JSON.stringify(body),
  });
  if (response.redirected || response.status === 401)
    throw new ApiError(401, "Your session has expired. Sign in to reconnect.");
  const data = (await response.json().catch(() => ({}))) as Record<
    string,
    unknown
  >;
  if (!response.ok)
    throw new ApiError(
      response.status,
      typeof data.detail === "string" ? data.detail : JSON.stringify(data),
    );
  return data as T;
}
export function query(
  filters: Filters,
  extra: Record<string, string | number> = {},
) {
  const params = new URLSearchParams();
  Object.entries({ ...filters, ...extra }).forEach(([key, value]) => {
    if (value !== "") params.set(key, String(value));
  });
  return params.toString();
}
