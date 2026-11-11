// Typed client for the coordinator's /api/v1 contract. The types in schema.d.ts
// are generated from openapi/schema.json together with the web client's.
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Task = Schemas["Task"];
export type TaskDetail = Schemas["TaskDetailResponse"];

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** A client for one server, sending the device token (`Authorization: Token`). */
export function createApi(baseUrl: string, token?: string | null) {
  const client = createClient<paths>({
    baseUrl: baseUrl.replace(/\/+$/, ""),
    headers: { Accept: "application/json" },
  });
  if (token) {
    const auth: Middleware = {
      onRequest({ request }) {
        request.headers.set("Authorization", `Token ${token}`);
        return request;
      },
    };
    client.use(auth);
  }
  return client;
}

export type Api = ReturnType<typeof createApi>;

/** The server's message: `detail`, or the first field error ("field: message"). */
export function errorMessage(error: unknown): string | undefined {
  if (!error || typeof error !== "object") return undefined;
  const body = error as Record<string, unknown>;
  if (typeof body.detail === "string") return body.detail;
  for (const [field, value] of Object.entries(body)) {
    const first = Array.isArray(value) ? value[0] : value;
    if (typeof first === "string")
      return field === "non_field_errors" ? first : `${field}: ${first}`;
  }
  return undefined;
}

/** Return the body or throw an ApiError with the server's message. */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await request;
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check the address and your network.");
  }
  const { data, error, response } = result;
  if (!response.ok || data === undefined) {
    throw new ApiError(
      response.status,
      errorMessage(error) ?? `Request failed (${response.status})`,
    );
  }
  return data;
}
