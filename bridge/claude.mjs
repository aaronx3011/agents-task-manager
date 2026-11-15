// Translate the Claude Agent SDK (Claude Code as a library) into the
// coordinator worker contract (issue #98). One streaming-input query per
// dashboard session; tool approvals and AskUserQuestion wait as pending cards.
import { randomUUID } from "node:crypto"
import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs"
import { dirname, isAbsolute, join, relative } from "node:path"
import { fileURLToPath } from "node:url"

const PREVIEW_LIMIT = 6000
const preview = (value) => String(value ?? "").slice(0, PREVIEW_LIMIT)
const modelCard = (modelID, name = modelID) => ({ providerID: "anthropic", modelID, name })
const FALLBACK_MODELS = [modelCard("default", "Default"), modelCard("opus", "Opus"),
  modelCard("sonnet", "Sonnet"), modelCard("haiku", "Haiku")]
const QUESTION_TOOL = "AskUserQuestion"
const WINDOW_MINUTES = { five_hour: 300, seven_day: 10080, seven_day_opus: 10080, seven_day_sonnet: 10080,
  seven_day_overage_included: 10080 }

// Subscription rate-limit events -> the worker usage shape shared with Codex:
// { sampled_at, windows: [{ remaining_percent, duration_minutes, resets_at }] }.
// `utilization` is a 0–1 fraction; `unifiedWindows` (observed with SDK
// 0.3.316, not in its types) lists every window, the top level only one.
export function usageFromRateLimit(info, now = Date.now()) {
  if (!info || typeof info !== "object") return undefined
  const entries = info.unifiedWindows && typeof info.unifiedWindows === "object"
    ? Object.entries(info.unifiedWindows)
    : info.rateLimitType ? [[info.rateLimitType, info]] : []
  const windows = entries
    .filter(([kind, window]) => kind in WINDOW_MINUTES && Number.isFinite(window?.utilization) &&
      window.utilization >= 0 && window.utilization <= 1)
    .map(([kind, window]) => ({
      kind,
      remaining_percent: Math.round((1 - window.utilization) * 1000) / 10,
      duration_minutes: WINDOW_MINUTES[kind],
      resets_at: Number.isFinite(window.resetsAt) ? window.resetsAt : null,
    }))
  return windows.length ? { sampled_at: Math.floor(now / 1000), windows } : undefined
}

// A push-driven async iterable: the SDK pulls user turns from it for the
// lifetime of the session's query.
export class InputQueue {
  constructor() {
    this.items = []
    this.waiters = []
    this.closed = false
  }

  push(item) {
    if (this.closed) throw new Error("Claude session input is closed")
    const waiter = this.waiters.shift()
    if (waiter) waiter({ value: item, done: false })
    else this.items.push(item)
  }

  close() {
    this.closed = true
    for (const waiter of this.waiters.splice(0)) waiter({ value: undefined, done: true })
  }

  [Symbol.asyncIterator]() {
    return {
      next: () => {
        if (this.items.length) return Promise.resolve({ value: this.items.shift(), done: false })
        if (this.closed) return Promise.resolve({ value: undefined, done: true })
        return new Promise((resolve) => this.waiters.push(resolve))
      },
      return: () => {
        this.close()
        return Promise.resolve({ value: undefined, done: true })
      },
    }
  }
}

function toolOutput(content) {
  if (typeof content === "string") return content
  if (!Array.isArray(content)) return ""
  return content.map((block) => (block?.type === "text" ? block.text : `[${block?.type ?? "content"}]`)).join("\n")
}

function userText(content) {
  if (typeof content === "string") return content
  if (!Array.isArray(content)) return ""
  return content.filter((block) => block?.type === "text").map((block) => block.text).join("\n")
}

export class ClaudeConnection {
  constructor(options) {
    this.options = options
    this.records = new Map()
    this.live = new Map()
    this.active = new Set()
    this.history = new Map()
    this.blocks = new Map()
    this.tools = new Map()
    this.pending = new Map()
    this.hydrated = new Set()
    this.models = FALLBACK_MODELS
    this.registryDirectory = join(options.stateDirectory, "claude")
    this.file = join(this.registryDirectory, "sessions.json")
    this.root = realpathSync(options.workspaceRoot ?? options.directory ?? options.createDirectory ?? process.cwd())
    this.sdk = options.claudeSDK
    if (existsSync(this.file)) {
      for (const { session, claudeSessionID } of JSON.parse(readFileSync(this.file, "utf8"))) {
        this.records.set(session.id, { session, claudeSessionID })
      }
    }
    // Usage only arrives during turns; keep the last sample across restarts.
    this.usageFile = join(this.registryDirectory, "usage.json")
    try {
      if (existsSync(this.usageFile)) this.usage = JSON.parse(readFileSync(this.usageFile, "utf8"))
    } catch {
      this.usage = undefined
    }
  }

  saveUsage() {
    mkdirSync(this.registryDirectory, { recursive: true, mode: 0o700 })
    const scratch = `${this.usageFile}.${randomUUID()}.tmp`
    writeFileSync(scratch, JSON.stringify(this.usage), { mode: 0o600 })
    renameSync(scratch, this.usageFile)
  }

  save() {
    mkdirSync(this.registryDirectory, { recursive: true, mode: 0o700 })
    const scratch = `${this.file}.${randomUUID()}.tmp`
    writeFileSync(scratch, JSON.stringify([...this.records.values()].map(({ session, claudeSessionID }) =>
      ({ session, claudeSessionID }))), { mode: 0o600 })
    renameSync(scratch, this.file)
  }

  directory(value) {
    const directory = realpathSync(value || this.options.createDirectory || this.options.directory || this.root)
    const path = relative(this.root, directory)
    if (path === ".." || path.startsWith("../") || isAbsolute(path) ||
      (this.options.directory && directory !== this.options.directory)) {
      throw new Error("Requested directory is outside this Claude worker's workspace")
    }
    return directory
  }

  async loadSDK() {
    if (!this.sdk) this.sdk = await import("@anthropic-ai/claude-agent-sdk")
    if (!this.version) {
      try {
        // The package's exports map hides package.json; read it beside the entry.
        const entry = fileURLToPath(import.meta.resolve("@anthropic-ai/claude-agent-sdk"))
        this.version = `SDK ${JSON.parse(readFileSync(join(dirname(entry), "package.json"), "utf8")).version}`
      } catch {
        this.version = "agent-sdk"
      }
    }
    return this.sdk
  }

  // There is no long-lived local server: each session owns a query. This
  // loop only verifies the SDK and sign-in, then stays up until shutdown.
  async stream(onFrame, ready) {
    this.onFrame = onFrame
    try {
      await this.loadSDK()
      await this.probe()
      this.error = undefined
    } catch (error) {
      this.error = error.code === "ERR_MODULE_NOT_FOUND"
        ? "Claude Agent SDK is not installed (run `npm ci --prefix bridge`)"
        : `Claude Code is unavailable: ${error.message}`
      throw new Error(this.error)
    }
    this.ready = true
    for (const session of this.snapshot().sessions) {
      if (this.records.get(session.id).claudeSessionID) this.emit(session.id, "session.status", { status: { type: "idle" } })
    }
    ready()
    const signal = this.options.signal
    try {
      if (signal && !signal.aborted) await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }))
    } finally {
      this.ready = false
      for (const id of [...this.live.keys()]) this.closeSession(id)
    }
  }

  // Start one throwaway query (no prompt is ever sent) to read the model
  // catalog and confirm Claude Code is signed in on this machine.
  async probe() {
    const input = new InputQueue()
    const probe = this.sdk.query({ prompt: input, options: {
      cwd: this.root, ...(this.options.claudeExecutable ? { pathToClaudeCodeExecutable: this.options.claudeExecutable } : {}),
      persistSession: false,
    } })
    try {
      const account = await probe.accountInfo?.()
      const credential = (value) => value && value !== "none"
      if (account && (account.apiProvider ?? "firstParty") === "firstParty" &&
        !credential(account.apiKeySource) && !credential(account.tokenSource)) {
        throw new Error("Claude Code is not signed in for the worker's OS user " +
          "(set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, ANTHROPIC_API_KEY, " +
          "or a Bedrock/Vertex/Foundry provider in the worker's environment)")
      }
      const models = await probe.supportedModels?.()
      if (Array.isArray(models) && models.length) {
        this.models = models.map((model) => modelCard(model.value, model.displayName ?? model.value))
      }
    } finally {
      input.close()
      probe.close?.()
    }
  }

  health() {
    if (!this.ready) throw new Error(this.error ?? "Local Claude Code runtime is unavailable")
    return { version: this.version }
  }

  async catalog() {
    return this.models
  }

  snapshot() {
    const records = [...this.records.values()].filter(({ session }) =>
      !this.options.directory || session.directory === this.options.directory)
    const selected = records.sort((a, b) => b.session.time.updated - a.session.time.updated)
      .slice(0, this.options.sessionLimit)
    for (const { card } of this.pending.values()) {
      const record = this.records.get(card.sessionID)
      if (record && selected.length < 100 && !selected.includes(record)) selected.push(record)
    }
    const sessions = selected.map(({ session }) => ({ ...session, status: this.active.has(session.id) ? "busy" : "idle" }))
    const ids = new Set(sessions.map((session) => session.id))
    const pending = [...this.pending.values()]
    const permissions = pending.filter(({ kind, card }) => kind === "permission" && ids.has(card.sessionID))
    const questions = pending.filter(({ kind, card }) => kind === "question" && ids.has(card.sessionID))
    return { sessions, pending: {
      complete: permissions.length + questions.length === pending.length && permissions.length <= 200 && questions.length <= 200,
      permissions: permissions.slice(0, 200).map(({ card }) => card),
      questions: questions.slice(0, 200).map(({ card }) => card),
    } }
  }

  async messages(session, limit) {
    const record = this.records.get(session.id)
    if (record?.claudeSessionID && !this.hydrated.has(session.id) && !this.live.has(session.id)) {
      const sdk = await this.loadSDK()
      const transcript = await sdk.getSessionMessages(record.claudeSessionID, { dir: session.directory })
      if (!this.hydrated.has(session.id) && !this.live.has(session.id)) {
        this.history.delete(session.id)
        for (const message of transcript ?? []) {
          if (message.parent_tool_use_id) continue
          this.putMessage(session.id, { type: message.type, uuid: message.uuid, message: message.message })
        }
        this.hydrated.add(session.id)
      }
    }
    return [...(this.history.get(session.id)?.values() ?? [])].slice(-limit)
  }

  emit(sessionID, type, properties = {}) {
    const record = this.records.get(sessionID)
    this.onFrame?.({
      directory: record?.session.directory,
      payload: { type, properties: { ...properties, sessionID } },
    })
  }

  touch(sessionID) {
    const record = this.records.get(sessionID)
    if (record) record.session.time.updated = Date.now()
  }

  setPart(sessionID, key, role, part) {
    const history = this.history.get(sessionID) ?? new Map()
    const previous = history.get(key)
    const latest = [...history.values()].at(-1)?.info.time.created ?? 0
    const created = previous?.info.time.created ?? Math.max(Date.now(), latest + 1)
    history.set(key, {
      info: { id: key, sessionID, role, time: { created } },
      parts: [{ ...part, id: key, messageID: key, sessionID }],
    })
    while (history.size > this.options.messageLimit) history.delete(history.keys().next().value)
    this.history.set(sessionID, history)
    return history.get(key)
  }

  // Assistant content block -> one dashboard message, keyed by API message id
  // and block position so streamed deltas and the final block share a row.
  putBlock(sessionID, key, block) {
    if (block.type === "text") return this.setPart(sessionID, key, "assistant", { type: "text", text: preview(block.text) })
    if (block.type === "thinking") return this.setPart(sessionID, key, "assistant", { type: "reasoning", text: preview(block.thinking) })
    if (block.type === "tool_use" || block.type === "server_tool_use" || block.type === "mcp_tool_use") {
      if (block.id) this.tools.set(block.id, { sessionID, key })
      const existing = this.history.get(sessionID)?.get(key)?.parts[0]
      return this.setPart(sessionID, key, "assistant", { type: "tool", tool: preview(block.name ?? "tool"), state: {
        status: existing?.state?.status ?? "running",
        input: block.input && typeof block.input === "object" ? block.input : {},
        output: existing?.state?.output ?? "",
      } })
    }
  }

  putMessage(sessionID, message) {
    const content = message.message?.content
    if (message.type === "user") {
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block?.type !== "tool_result") continue
          const target = this.tools.get(block.tool_use_id)
          const part = target && this.history.get(target.sessionID)?.get(target.key)?.parts[0]
          if (part?.type === "tool") {
            part.state.status = block.is_error ? "error" : "completed"
            part.state.output = preview(toolOutput(block.content))
          }
        }
      }
      const text = userText(content)
      if (text && !message.isSynthetic) this.setPart(sessionID, message.uuid ?? randomUUID(), "user", { type: "text", text: preview(text) })
      return
    }
    if (message.type !== "assistant" || !Array.isArray(content)) return
    const apiID = message.message.id ?? message.uuid
    const counts = this.blocks.get(sessionID) ?? new Map()
    for (const block of content) {
      const index = counts.get(apiID) ?? 0
      counts.set(apiID, index + 1)
      this.putBlock(sessionID, `${apiID}:${index}`, block)
    }
    this.blocks.set(sessionID, counts)
  }

  observe(sessionID, message) {
    const record = this.records.get(sessionID)
    if (!record) return
    if (message.session_id && record.claudeSessionID !== message.session_id) {
      record.claudeSessionID = message.session_id
      this.save()
    }
    if (message.type === "rate_limit_event") {
      // Account-wide, so any session's event refreshes the worker's usage.
      const usage = usageFromRateLimit(message.rate_limit_info)
      if (usage) {
        this.usage = usage
        this.saveUsage()
      }
      return
    }
    if (message.parent_tool_use_id) return
    if (message.type === "stream_event") {
      this.markBusy(sessionID)
      this.streamEvent(sessionID, message.event)
      return
    }
    if (message.type === "assistant" || message.type === "user") {
      if (message.type === "assistant") this.markBusy(sessionID)
      this.putMessage(sessionID, message)
      this.touch(sessionID)
      this.emit(sessionID, "message.part.updated")
      return
    }
    if (message.type === "system" && message.subtype === "session_state_changed") {
      if (message.state === "idle") this.markIdle(sessionID)
      else this.markBusy(sessionID)
      return
    }
    if (message.type === "result") {
      if (message.subtype !== "success" || message.is_error) {
        const detail = Array.isArray(message.errors) && message.errors.length ? message.errors.join("; ")
          : typeof message.result === "string" && message.result ? message.result : message.subtype
        this.emit(sessionID, "session.error", { error: { message: preview(`Claude turn failed: ${detail}`) } })
      }
      this.markIdle(sessionID)
    }
  }

  streamEvent(sessionID, event) {
    if (!event) return
    const state = this.streaming?.get(sessionID)
    if (event.type === "message_start") {
      this.streaming ??= new Map()
      this.streaming.set(sessionID, { apiID: event.message?.id })
      return
    }
    if (!state?.apiID) return
    if (event.type === "content_block_start") {
      // Final assistant frames arrive one block at a time, in block order, so
      // the streamed block index and the final block's position share a key.
      state.key = `${state.apiID}:${event.index ?? 0}`
      this.putBlock(sessionID, state.key, event.content_block ?? {})
      this.emit(sessionID, "message.part.updated")
    } else if (event.type === "content_block_delta" && state.key) {
      const part = this.history.get(sessionID)?.get(state.key)?.parts[0]
      const delta = event.delta ?? {}
      if (part && delta.type === "text_delta") part.text = preview(part.text + delta.text)
      else if (part && delta.type === "thinking_delta") part.text = preview(part.text + delta.thinking)
      else return
      this.emit(sessionID, "message.part.delta")
    }
  }

  markBusy(sessionID) {
    if (this.active.has(sessionID)) return
    this.active.add(sessionID)
    this.emit(sessionID, "session.status", { status: { type: "busy" } })
  }

  markIdle(sessionID) {
    this.touch(sessionID)
    this.save()
    if (!this.active.delete(sessionID)) return
    this.emit(sessionID, "session.status", { status: { type: "idle" } })
  }

  // Tool approvals and AskUserQuestion both arrive here; the promise stays
  // open until the dashboard replies or the SDK aborts the request.
  canUseTool(sessionID, toolName, input, { signal, suggestions, title, displayName, decisionReason, blockedPath } = {}) {
    return new Promise((resolve) => {
      const id = randomUUID()
      let card
      let kind
      if (toolName === QUESTION_TOOL && Array.isArray(input?.questions) && input.questions.length) {
        kind = "question"
        card = { id, sessionID, questions: input.questions.map((q) => ({
          header: preview(q.header), text: preview(q.question),
          options: (q.options ?? []).map((o) => ({ label: preview(o.label), description: preview(o.description) })),
          multiple: q.multiSelect === true, custom: true,
        })) }
      } else {
        kind = "permission"
        const pattern = input?.command ?? input?.file_path ?? input?.url ?? input?.pattern ?? blockedPath
        card = {
          id, sessionID, type: toolName === "Bash" ? "command" : ["Edit", "Write", "NotebookEdit"].includes(toolName) ? "file" : "tool",
          title: preview(title ?? `Claude wants to use ${displayName ?? toolName}`),
          description: preview(decisionReason ?? ""),
          patterns: [pattern].filter((value) => typeof value === "string" && value).map(preview),
        }
      }
      const settle = (result) => {
        if (!this.pending.has(id)) return
        this.pending.delete(id)
        this.emit(sessionID, `${kind}.replied`)
        resolve(result)
      }
      this.pending.set(id, { kind, input, suggestions, card, settle })
      signal?.addEventListener("abort", () => settle({ behavior: "deny", message: "Request cancelled" }), { once: true })
      this.emit(sessionID, `${kind}.asked`)
    })
  }

  startSession(sessionID) {
    const record = this.records.get(sessionID)
    const input = new InputQueue()
    const abortController = new AbortController()
    const model = record.session.model?.modelID
    const query = this.sdk.query({ prompt: input, options: {
      cwd: this.directory(record.session.directory),
      ...(model && model !== "default" ? { model } : {}),
      ...(record.claudeSessionID ? { resume: record.claudeSessionID } : {}),
      ...(this.options.claudeExecutable ? { pathToClaudeCodeExecutable: this.options.claudeExecutable } : {}),
      permissionMode: this.options.claudePermissionMode ?? "default",
      includePartialMessages: true,
      abortController,
      canUseTool: (toolName, toolInput, details) => this.canUseTool(sessionID, toolName, toolInput, details),
    } })
    const live = { input, query, abortController }
    this.live.set(sessionID, live)
    this.hydrated.add(sessionID)
    live.done = (async () => {
      try {
        for await (const message of query) this.observe(sessionID, message)
      } catch (error) {
        if (!abortController.signal.aborted) {
          this.emit(sessionID, "session.error", { error: { message: preview(`Claude session stopped: ${error.message}`) } })
        }
      } finally {
        if (this.live.get(sessionID) === live) this.closeSession(sessionID)
      }
    })()
    return live
  }

  closeSession(sessionID) {
    const live = this.live.get(sessionID)
    if (!live) return
    this.live.delete(sessionID)
    live.input.close()
    live.abortController.abort()
    live.query.close?.()
    for (const request of [...this.pending.values()]) {
      if (request.card.sessionID === sessionID) request.settle({ behavior: "deny", message: "Session closed" })
    }
    this.markIdle(sessionID)
  }

  async execute(command, sessions) {
    const payload = command.payload ?? {}
    const sessionID = payload.sessionID ?? command.sessionID
    if (command.action === "create") {
      const id = randomUUID()
      const directory = this.directory(payload.directory)
      const time = Date.now()
      const session = { id, directory, title: payload.title || "Dashboard session",
        model: payload.model ?? this.options.model, time: { created: time, updated: time } }
      this.records.set(id, { session })
      this.save()
      return session
    }
    if (!sessions.some(({ id }) => id === sessionID)) throw new Error("Session is no longer available on this worker")
    const record = this.records.get(sessionID)
    if (!record) throw new Error("Session is no longer available on this worker")
    if (command.action === "prompt") {
      if (payload.noReply) throw new Error("Claude requires a reply for each prompt")
      if (typeof payload.text !== "string" || !payload.text.trim()) throw new Error("Prompt text is required")
      await this.loadSDK()
      if (payload.model?.modelID && payload.model.modelID !== record.session.model?.modelID) {
        record.session.model = payload.model
        this.save()
        await this.live.get(sessionID)?.query.setModel?.(payload.model.modelID === "default" ? undefined : payload.model.modelID)
      }
      const live = this.live.get(sessionID) ?? this.startSession(sessionID)
      const uuid = randomUUID()
      live.input.push({ type: "user", uuid, parent_tool_use_id: null,
        message: { role: "user", content: payload.text } })
      this.setPart(sessionID, uuid, "user", { type: "text", text: preview(payload.text) })
      this.markBusy(sessionID)
      return { id: uuid }
    }
    if (command.action === "interrupt") {
      const live = this.live.get(sessionID)
      if (!live || !this.active.has(sessionID)) throw new Error("No Claude turn is running")
      await live.query.interrupt()
      return {}
    }
    const requestID = payload.requestID ?? command.requestID
    const request = this.pending.get(requestID)
    if (!request || request.card.sessionID !== sessionID || !command.action.startsWith(request.kind + ".")) {
      throw new Error("This Claude request is no longer pending on this session")
    }
    if (request.kind === "permission") {
      if (payload.reply === "once") request.settle({ behavior: "allow", updatedInput: request.input })
      else if (payload.reply === "always") {
        request.settle({ behavior: "allow", updatedInput: request.input,
          ...(request.suggestions?.length ? { updatedPermissions: request.suggestions } : {}) })
      } else if (payload.reply === "reject") request.settle({ behavior: "deny", message: "The user declined this action" })
      else throw new Error("This decision is unavailable for this Claude approval")
    } else if (command.action === "question.reject") {
      request.settle({ behavior: "deny", message: "The user declined to answer" })
    } else {
      const answers = payload.answers
      if (!Array.isArray(answers) || answers.length !== request.input.questions.length) {
        throw new Error("Answer every question")
      }
      request.settle({ behavior: "allow", updatedInput: { ...request.input, answers: Object.fromEntries(
        request.input.questions.map((question, index) => [question.question, answers[index].join(", ")])) } })
    }
    return {}
  }
}
