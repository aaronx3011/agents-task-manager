// Local OpenCode client for the installed v1 API (verified with 1.18.32).
// Adapted from the dashboard bridge `src/opencode.mjs`; command inputs follow
// the Django `worker_command_card` payload contract (issue #12/#16).
export class OpenCodeConnection {
  constructor({ url, directory, createDirectory, model, password, username = "opencode", signal }) {
    this.url = url
    this.directory = directory
    this.createDirectory = createDirectory
    this.model = model
    this.signal = signal
    this.headers = password
      ? { Authorization: `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}` }
      : {}
  }

  endpoint(path, directory = this.directory) {
    const url = new URL(path, this.url)
    if (directory) url.searchParams.set("directory", directory)
    return url
  }

  request(path, { directory = this.directory, body, ...options } = {}) {
    return jsonRequest(this.endpoint(path, directory), {
      ...options,
      ...(this.signal ? { signal: this.signal } : {}),
      headers: { ...this.headers, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  }

  // Flatten a Django command card into one local OpenCode call. OpenCode owns
  // permission/order enforcement locally; the bridge re-checks pending state
  // against the snapshot it published before replying.
  async execute(command, sessions) {
    const payload = command.payload ?? {}
    if (command.action === "create") {
      const model = payload.model ?? this.model
      return this.request("/session", {
        method: "POST",
        directory: payload.directory || this.createDirectory || this.directory,
        body: { title: payload.title || "Dashboard session", ...(model ? { model: { providerID: model.providerID, id: model.modelID } } : {}) },
      })
    }
    const sessionID = payload.sessionID ?? command.sessionID
    const session = sessions.find((s) => s.id === sessionID)
    if (!session) throw new Error("Session is no longer available on this worker")
    const requestID = payload.requestID ?? command.requestID
    if (["permission.reply", "question.reply", "question.reject"].includes(command.action)) {
      const requests = await this.request(command.action === "permission.reply" ? "/permission" : "/question", {
        directory: session.directory,
      })
      if (!requests.some((r) => r.id === requestID && r.sessionID === session.id)) {
        throw new Error("This request is no longer pending on this session")
      }
      if (command.action === "permission.reply") {
        return this.request(`/permission/${encodeURIComponent(requestID)}/reply`, {
          method: "POST",
          directory: session.directory,
          body: { reply: payload.reply, ...(payload.message ? { message: payload.message } : {}) },
        })
      }
      if (command.action === "question.reply") {
        return this.request(`/question/${encodeURIComponent(requestID)}/reply`, {
          method: "POST",
          directory: session.directory,
          body: { answers: payload.answers },
        })
      }
      return this.request(`/question/${encodeURIComponent(requestID)}/reject`, {
        method: "POST",
        directory: session.directory,
      })
    }
    const path = `/session/${encodeURIComponent(session.id)}`
    if (command.action === "interrupt") {
      return this.request(`${path}/abort`, { method: "POST", directory: session.directory })
    }
    if (command.action === "prompt") {
      return this.request(`${path}/prompt_async`, {
        method: "POST",
        directory: session.directory,
        body: {
          parts: [{ type: "text", text: payload.text }],
          noReply: payload.noReply ?? false,
          ...((payload.model ?? this.model) ? { model: payload.model ?? this.model } : {}),
        },
      })
    }
    throw new Error("Unsupported action")
  }
}

// SSE frames can span arbitrary TCP chunks, including in the middle of UTF-8 characters.
export async function consumeSSE(body, onEvent) {
  const decoder = new TextDecoder()
  let buffer = ""
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true }).replace(/\r/g, "")
    let end
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n")
      if (data) onEvent(JSON.parse(data))
    }
  }
}

export async function jsonRequest(url, options = {}) {
  const { signal, ...request } = options
  const timeout = AbortSignal.timeout(15000)
  const response = await fetch(url, { ...request, redirect: "error", signal: signal ? AbortSignal.any([signal, timeout]) : timeout })
  // Response bodies may contain provider/auth details; do not forward them to the hub.
  if (!response.ok) throw new Error(`Local OpenCode HTTP ${response.status}`)
  if (response.status === 204) return null
  const text = await response.text()
  if (text && !response.headers.get("content-type")?.includes("application/json")) {
    throw new Error("Unsupported OpenCode API response: expected JSON")
  }
  return text ? JSON.parse(text) : null
}
