// Ephemeral full-state overlays; durable writes/commands remain in HTTP sync.
export class LiveSnapshots {
  constructor({ coordinatorURL, worker, key, signal, connection, sessions, messageLimit, wake }) {
    this.options = { coordinatorURL, worker, key, signal, connection, sessions, messageLimit, wake }
    this.dirty = new Set()
    this.states = new Map()
    this.bytes = 0
    this.busy = false
    this.ready = false
    this.enabled = typeof WebSocket !== "undefined"
    this.refresh = setInterval(() => void this.flush(), 100)
    this.heartbeat = setInterval(() => {
      if (this.ready) this.send({ type: "ping" })
    }, 10000)
    signal.addEventListener("abort", () => this.stop(), { once: true })
  }

  enable() {
    if (this.started || !this.enabled || this.options.signal.aborted) return
    this.started = true
    this.connect()
  }

  connect() {
    if (!this.enabled || this.options.signal.aborted) return
    const url = new URL("/ws/worker/", this.options.coordinatorURL)
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
    const socket = this.socket = new WebSocket(url)
    socket.addEventListener("open", () => socket.send(JSON.stringify({
      type: "authenticate", worker: this.options.worker, key: this.options.key,
    })))
    socket.addEventListener("message", (event) => {
      try {
        const message = JSON.parse(event.data)
        if (message.type === "ready" && message.protocol_version === 1) {
          this.ready = true
          for (const id of this.options.sessions.keys()) this.mark(id)
          this.options.wake()
        }
        if (message.type === "sync") this.options.wake()
      } catch { socket.close() }
    })
    socket.addEventListener("error", () => socket.close())
    socket.addEventListener("close", () => {
      this.ready = false
      if (!this.options.signal.aborted) this.retry = setTimeout(() => this.connect(), 2000)
    })
  }

  send(value) {
    if (!this.ready || this.socket?.readyState !== WebSocket.OPEN) return false
    const body = JSON.stringify(value)
    if (Buffer.byteLength(body) > 256 * 1024 || this.socket.bufferedAmount > 512 * 1024) {
      // Never grow an unbounded live queue. HTTP snapshots recover omitted data.
      if (value.type === "snapshot" && this.socket.bufferedAmount <= 512 * 1024) {
        this.socket.send(JSON.stringify({ type: "gap", sessionID: value.sessionID }))
      } else this.socket.close()
      return false
    }
    this.socket.send(body)
    return true
  }

  mark(sessionID) {
    if (sessionID && this.dirty.size < 100) this.dirty.add(sessionID)
  }

  observe(event, sessionID) {
    if (event.type === "session.status" && sessionID && (this.states.has(sessionID) || this.states.size < 100)) {
      this.states.set(sessionID, event.properties?.status?.type)
      this.mark(sessionID)
    }
    if (event.type.startsWith("message.")) {
      this.mark(sessionID)
      this.bytes += Buffer.byteLength(JSON.stringify(event))
      if (this.bytes >= 256 * 1024) {
        this.bytes = 0
        this.options.wake()
      }
    }
    const terminal = event.type === "session.idle" || event.type === "session.error" ||
      (event.type === "session.status" && event.properties?.status?.type === "idle") ||
      (event.type === "message.updated" && event.properties?.info?.time?.completed)
    if (terminal || event.type.startsWith("permission.") || event.type.startsWith("question.")) {
      this.mark(sessionID)
      this.options.wake()
    }
  }

  async flush() {
    if (this.busy || !this.ready || this.options.signal.aborted) return
    this.busy = true
    try {
      for (const id of [...this.dirty].slice(0, 8)) {
        this.dirty.delete(id)
        const session = this.options.sessions.get(id)
        if (!session) continue
        const history = await this.options.connection.request(
          `/session/${encodeURIComponent(id)}/message?limit=${this.options.messageLimit}`,
          { directory: session.directory },
        )
        if (!Array.isArray(history)) continue
        this.send({ type: "snapshot", sessionID: id, messages: history,
          executionStatus: this.states.get(id) ?? session.status })
      }
    } catch {
      // Local history remains canonical; polling and reconnect reconcile failures.
    } finally { this.busy = false }
  }

  stop() {
    clearInterval(this.refresh)
    clearInterval(this.heartbeat)
    clearTimeout(this.retry)
    this.socket?.close()
    this.dirty.clear()
    this.states.clear()
  }
}

// Wakes are coalesced, including those received during an HTTP upload. There is
// still exactly one HTTP sync in flight; immutable retries cannot be bypassed.
export function syncScheduler(signal) {
  let next = new AbortController()
  return {
    wake: () => next.abort(),
    async wait(milliseconds) {
      if (!next.signal.aborted && !signal.aborted) {
        await new Promise((resolve) => {
          const finish = () => { clearTimeout(timer); combined.removeEventListener("abort", finish); resolve() }
          const combined = AbortSignal.any([signal, next.signal])
          const timer = setTimeout(finish, milliseconds)
          combined.addEventListener("abort", finish, { once: true })
          if (combined.aborted) finish()
        })
      }
      next = new AbortController()
    },
  }
}
