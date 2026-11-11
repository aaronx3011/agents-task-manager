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
    const detail =
      error && typeof error === "object" && "detail" in error
        ? (error as { detail: unknown }).detail
        : undefined;
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : `Request failed (${response.status})`,
    );
  }
  return data;
}
