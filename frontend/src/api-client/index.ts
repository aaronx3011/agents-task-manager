// Typed client for the /api/v1 contract. Types are generated from the committed
// openapi/schema.json (npm run api:generate); never edit schema.d.ts by hand.
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type { components, paths };
export type Schemas = components["schemas"];

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export const SESSION_EXPIRED =
  "Your session has expired. Sign in to reconnect.";

function csrfToken() {
  if (typeof document === "undefined") return "";
  const cookie = document.cookie
    .split("; ")
    .find((item) => item.startsWith("csrftoken="));
  return cookie ? decodeURIComponent(cookie.slice("csrftoken=".length)) : "";
}

const csrf: Middleware = {
  onRequest({ request }) {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method))
      request.headers.set("X-CSRFToken", csrfToken());
    return request;
  },
};

export interface ApiClientOptions {
  baseUrl?: string;
  headers?: Record<string, string>;
  fetch?: typeof fetch;
  /** Send Django's CSRF header from the csrftoken cookie (browser sessions). */
  sessionCsrf?: boolean;
}

export function createApiClient(options: ApiClientOptions = {}) {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? "",
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json", ...options.headers },
    fetch: options.fetch,
  });
  if (options.sessionCsrf) client.use(csrf);
  return client;
}

/** Same-origin browser client using the Django session cookie. */
export const api = createApiClient({ sessionCsrf: true });

/** Return the response body or throw an ApiError, matching the legacy get/post helpers. */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await request;
  if (response.redirected || response.status === 401 || response.status === 403)
    throw new ApiError(401, SESSION_EXPIRED);
  if (!response.ok || data === undefined) {
    const detail =
      error && typeof error === "object" && "detail" in error
        ? (error as { detail: unknown }).detail
        : undefined;
    throw new ApiError(
      response.status,
      typeof detail === "string"
        ? detail
        : `Request failed (${response.status})`,
    );
  }
  return data;
}
