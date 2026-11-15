// Translate the Codex App Server protocol into the coordinator worker contract.
import { randomUUID } from "node:crypto"
import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs"
import { isAbsolute, join, relative } from "node:path"
import { once } from "node:events"
import { CodexRPC } from "./codex-rpc.mjs"

const PREVIEW_LIMIT = 6000
const USAGE_REFRESH_MS = 60000
const preview = (value) => String(value ?? "").slice(0, PREVIEW_LIMIT)
const modelCard = (modelID) => ({ providerID: "openai", modelID })
const decisions = { once: ["accept"], always: ["acceptForSession"], reject: ["decline", "cancel"] }

function messagePart(item) {
  switch (item.type) {
    case "userMessage":
      return { type: "text", text: preview(item.content?.filter((part) => part.type === "text").map((part) => part.text).join("\n")) }
    case "agentMessage":
      return { type: "text", text: preview(item.text) }
    case "reasoning":
      return { type: "reasoning", text: preview(item.summary?.join("\n")) }
    case "commandExecution":
      return { type: "tool", tool: "bash", state: {
        status: ["completed", "failed", "declined"].includes(item.status) ?
          (item.status === "completed" ? "completed" : "error") : "running",
        input: { command: preview(item.command) },
        output: preview(item.aggregatedOutput),
      } }
    case "fileChange":
      return { type: "tool", tool: "apply_patch", state: {
        status: item.status === "completed" ? "completed" : item.status === "inProgress" ? "running" : "error",
        input: { paths: item.changes?.map((change) => change.path).slice(0, 40) ?? [] },
      } }
    case "mcpToolCall":
      return { type: "tool", tool: preview(`${item.server}/${item.tool}`), state: {
        status: item.status === "completed" ? "completed" : item.status === "inProgress" ? "running" : "error",
      } }
  }
}

export class CodexConnection {
  constructor(options) {
    this.options = options
    this.nextUsageRefresh = 0
    this.usage = undefined
    this.records = new Map()
    this.threads = new Map()
    this.active = new Map()
    this.loaded = new Set()
    this.history = new Map()
    this.pending = new Map()
    this.hydrated = new Set()
    this.registryDirectory = join(options.stateDirectory, "codex")
    this.file = join(this.registryDirectory, "sessions.json")
    this.root = realpathSync(options.workspaceRoot ?? options.directory ?? options.createDirectory ?? process.cwd())
    if (existsSync(this.file)) {
      for (const { session, threadID } of JSON.parse(readFileSync(this.file, "utf8"))) {
        this.records.set(session.id, { session, threadID })
        if (threadID) this.threads.set(threadID, session.id)
      }
    }
  }

  save() {
    mkdirSync(this.registryDirectory, { recursive: true, mode: 0o700 })
    const scratch = `${this.file}.${randomUUID()}.tmp`
    writeFileSync(scratch, JSON.stringify([...this.records.values()]), { mode: 0o600 })
    renameSync(scratch, this.file)
  }

  directory(value) {
    const directory = realpathSync(value || this.options.createDirectory || this.options.directory || this.root)
    const path = relative(this.root, directory)
    if (path === ".." || path.startsWith("../") || isAbsolute(path) ||
      (this.options.directory && directory !== this.options.directory)) {
      throw new Error("Requested directory is outside this Codex worker's workspace")
    }
    return directory
  }

  async stream(onFrame, ready) {
    const rpc = new CodexRPC({ binary: this.options.codexBinary ?? "codex", cwd: this.root, signal: this.options.signal })
    this.rpc = rpc
    const closed = once(rpc, "close")
    rpc.on("frame", (frame) => {
      try {
        this.observe(frame, onFrame)
      } catch {
        rpc.close("Could not process a Codex App Server event")
      }
    })
    try {
      const info = await rpc.call("initialize", { clientInfo: {
        name: "agent_task_coordinator", title: "Agent Task Coordinator", version: "1.0.0",
      } })
      rpc.send({ method: "initialized" })
      const account = await rpc.call("account/read", { refreshToken: false })
      if (account.requiresOpenaiAuth && !account.account) throw new Error("Codex CLI requires sign-in")
      this.version = info.userAgent?.match(/\/([0-9][^ ]*)/)?.[1] ?? "app-server"
      this.ready = true
      // A fresh subprocess has no running turns. Reconcile the coordinator
      // even when an interrupted turn's durable history timestamp is older.
      for (const session of this.snapshot().sessions) {
        if (this.records.get(session.id).threadID) onFrame({
          directory: session.directory,
          payload: { type: "session.status", properties: {
            sessionID: session.id, status: { type: "idle" },
          } },
        })
      }
      ready()
      await closed
    } finally {
      this.ready = false
      rpc.close("Codex connection stopped")
      this.pending.clear()
      this.loaded.clear()
      for (const id of this.active.keys()) this.records.get(id).session.time.updated = Date.now()
      this.active.clear()
      this.hydrated.clear()
    }
  }

  health() {
    if (!this.ready || this.rpc.closed) throw new Error("Local Codex App Server is unavailable")
    if (Date.now() >= this.nextUsageRefresh) {
      this.nextUsageRefresh = Date.now() + USAGE_REFRESH_MS
      void this.refreshUsage(this.rpc)
    }
    return { version: this.version }
  }

  async refreshUsage(rpc) {
    try {
      const result = await rpc.call("account/rateLimits/read", {}, {
        timeout: 5000,
        closeOnTimeout: false,
      })
      const limits = result.rateLimitsByLimitId?.codex ?? result.rateLimits
      const windows = [limits?.primary, limits?.secondary]
        .filter((window) => Number.isFinite(window?.usedPercent) &&
          window.usedPercent >= 0 && window.usedPercent <= 100)
        .map((window) => ({
          remaining_percent: 100 - window.usedPercent,
          duration_minutes: window.windowDurationMins ?? null,
          resets_at: window.resetsAt ?? null,
        }))
      if (this.rpc === rpc && !rpc.closed && windows.length) {
        this.usage = { sampled_at: Math.floor(Date.now() / 1000), windows }
      }
    } catch {
      // Usage can be unavailable even when the local runtime is healthy.
    }
  }

  async catalog() {
    const { data } = await this.rpc.call("model/list", { limit: 100 })
    return data.map((model) => ({ ...modelCard(model.model), name: model.displayName ?? model.model }))
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
    const { threadID } = this.records.get(session.id)
    if (threadID && !this.hydrated.has(session.id) && !this.loaded.has(session.id)) {
      const { thread } = await this.rpc.call("thread/read", { threadId: threadID, includeTurns: true })
      if (!this.hydrated.has(session.id) && !this.loaded.has(session.id)) {
        this.history.delete(session.id)
        for (const turn of thread.turns ?? []) {
          for (const item of turn.items) {
            this.putItem(session.id, item, turn.startedAt ? turn.startedAt * 1000 : session.time.created)
          }
        }
        this.hydrated.add(session.id)
      }
    }
    return [...(this.history.get(session.id)?.values() ?? [])].slice(-limit)
  }

  putItem(sessionID, item, time = Date.now()) {
    const part = messagePart(item)
    if (!part || !item.id) return
    const history = this.history.get(sessionID) ?? new Map()
    const previous = history.get(item.id)
    const latest = [...history.values()].at(-1)?.info.time.created ?? 0
    const created = previous?.info.time.created ?? Math.max(time, latest + 1)
    history.set(item.id, {
      info: { id: item.id, sessionID, role: item.type === "userMessage" ? "user" : "assistant", time: { created } },
      parts: [{ ...part, id: item.id, messageID: item.id, sessionID }],
    })
    while (history.size > this.options.messageLimit) history.delete(history.keys().next().value)
    this.history.set(sessionID, history)
  }

  observe({ id, method, params = {} }, onFrame) {
    const sessionID = this.threads.get(params.threadId)
    const record = this.records.get(sessionID)
    if (id !== undefined) {
      this.requestInteraction(id, method, params, record?.session.id, onFrame)
      return
    }
    if (!record) return
    const emit = (type, properties = {}) => onFrame({
      directory: record.session.directory,
      payload: { type, properties: { ...properties, sessionID } },
    })
    if (method === "serverRequest/resolved") {
      const resolved = [...this.pending.entries()].find(([, request]) => request.rpcID === params.requestId)
      if (resolved) {
        this.pending.delete(resolved[0])
        emit(`${resolved[1].kind}.replied`)
      }
    } else if (method === "turn/started") {
      this.active.set(sessionID, params.turn.id)
      emit("session.status", { status: { type: "busy" } })
    } else if (method === "turn/completed") {
      this.active.delete(sessionID)
      for (const [key, request] of this.pending) if (request.card.sessionID === sessionID) this.pending.delete(key)
      record.session.time.updated = Date.now()
      this.save()
      if (params.turn.status === "failed") emit("session.error", { error: { message: "Codex turn failed" } })
      emit("session.status", { status: { type: "idle" } })
    } else if (method === "item/started" || method === "item/completed") {
      this.putItem(sessionID, params.item)
      record.session.time.updated = Date.now()
      emit("message.part.updated")
    } else if (method === "item/agentMessage/delta" || method === "item/commandExecution/outputDelta") {
      const part = this.history.get(sessionID)?.get(params.itemId)?.parts[0]
      if (part?.type === "text") part.text = preview(part.text + params.delta)
      if (part?.type === "tool") part.state.output = preview(part.state.output + params.delta)
      if (part) emit("message.part.delta")
    }
  }

  requestInteraction(rpcID, method, params, sessionID, onFrame) {
    let kind
    let details
    if (sessionID && ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"].includes(method)) {
      kind = "permission"
      details = {
        type: params.command ? "command" : "file",
        title: params.networkApprovalContext ? "Codex network access" : "Codex approval",
        description: preview(params.reason),
        patterns: [params.networkApprovalContext?.host, params.command, params.grantRoot].filter(Boolean).map(preview),
      }
    } else if (sessionID && method === "item/tool/requestUserInput" && params.questions?.length) {
      kind = "question"
      details = { questions: params.questions.map((q) => ({
        header: q.header, text: q.question, options: q.options, custom: q.isOther === true || !q.options?.length,
      })) }
    } else {
      this.rpc.send({ id: rpcID, error: { code: -32601, message: "This request is not available in the coordinator" } })
      return
    }
    const id = randomUUID()
    this.pending.set(id, { rpcID, kind, params, card: { id, sessionID, ...details } })
    onFrame({ payload: { type: `${kind}.asked`, properties: { sessionID } } })
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
    if (command.action === "prompt") {
      if (payload.noReply) throw new Error("Codex requires a reply for each prompt")
      if (this.active.has(sessionID)) throw new Error("A Codex turn is already running")
      const model = (payload.model ?? record.session.model)?.modelID
      if (!record.threadID) {
        const { thread } = await this.rpc.call("thread/start", {
          cwd: this.directory(record.session.directory), model,
          approvalPolicy: "on-request", sandbox: "workspace-write",
        })
        record.threadID = thread.id
        this.threads.set(thread.id, sessionID)
        this.loaded.add(sessionID)
        this.save()
      } else if (!this.loaded.has(sessionID)) {
        // Hydrate before resuming so loaded history and streamed items cannot race.
        await this.messages(record.session, this.options.messageLimit)
        await this.rpc.call("thread/resume", { threadId: record.threadID, excludeTurns: true })
        this.loaded.add(sessionID)
      }
      const { turn } = await this.rpc.call("turn/start", {
        threadId: record.threadID, model, input: [{ type: "text", text: payload.text }],
      })
      return { id: turn.id }
    }
    if (command.action === "interrupt") {
      const turnId = this.active.get(sessionID)
      if (!turnId) throw new Error("No Codex turn is running")
      return this.rpc.call("turn/interrupt", { threadId: record.threadID, turnId })
    }
    const requestID = payload.requestID ?? command.requestID
    const request = this.pending.get(requestID)
    if (!request || request.card.sessionID !== sessionID || !command.action.startsWith(request.kind + ".")) {
      throw new Error("This Codex request is no longer pending on this session")
    }
    let result
    if (request.kind === "permission") {
      const available = request.params.availableDecisions
      const decision = decisions[payload.reply]?.find((choice) => !available || available.includes(choice))
      if (!decision) throw new Error("This decision is unavailable for this Codex approval")
      result = { decision }
    } else {
      result = { answers: Object.fromEntries(request.params.questions.map((question, index) => [
        question.id, { answers: command.action === "question.reject" ? [] : (payload.answers?.[index] ?? []) },
      ])) }
    }
    this.rpc.send({ id: request.rpcID, result })
    this.pending.delete(requestID)
    return {}
  }
}
