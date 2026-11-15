import { spawn } from "node:child_process"
import { EventEmitter } from "node:events"
import { createInterface } from "node:readline"

export class CodexRPC extends EventEmitter {
  constructor({ binary, cwd, signal }) {
    super()
    this.requests = new Map()
    this.nextID = 0
    this.closed = false
    // The app server and its tools do not need the coordinator credential.
    const { COORDINATOR_API_KEY: _key, ...env } = process.env
    this.process = spawn(binary, ["app-server", "--listen", "stdio://"], {
      cwd,
      env,
      stdio: ["pipe", "pipe", "ignore"],
    })
    const lines = createInterface({ input: this.process.stdout, crlfDelay: Infinity })
    lines.on("line", (line) => {
      let frame
      try {
        frame = JSON.parse(line)
      } catch {
        this.close("Invalid JSON from Codex App Server")
        return
      }
      if (frame.method) {
        this.emit("frame", frame)
      } else {
        const request = this.requests.get(frame.id)
        if (frame.error) request?.reject(new Error(`Codex ${request.method} failed (RPC ${frame.error.code})`))
        else request?.resolve(frame.result)
      }
    })
    this.process.stdin.on("error", () => this.close("Codex App Server input closed"))
    this.process.once("error", (error) => this.close(`Could not launch Codex App Server (${error.code})`))
    this.process.once("exit", () => this.close("Codex App Server exited"))
    const abort = () => this.close("Codex worker stopped")
    signal?.addEventListener("abort", abort, { once: true })
    this.once("close", () => signal?.removeEventListener("abort", abort))
    if (signal?.aborted) abort()
  }

  send(frame) {
    if (this.closed) throw new Error("Codex App Server connection closed")
    this.process.stdin.write(JSON.stringify(frame) + "\n")
  }

  call(method, params, { timeout = 12000, closeOnTimeout = true } = {}) {
    const id = ++this.nextID
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const message = `Codex ${method} timed out`
        if (closeOnTimeout) this.close(message)
        else reject(new Error(message))
      }, timeout)
      this.requests.set(id, { method, resolve, reject, timer })
      try {
        this.send({ id, method, params })
      } catch (error) {
        reject(error)
      }
    }).finally(() => {
      clearTimeout(this.requests.get(id)?.timer)
      this.requests.delete(id)
    })
  }

  close(reason) {
    if (this.closed) return
    this.closed = true
    for (const request of this.requests.values()) request.reject(new Error(reason))
    this.process.kill()
    this.emit("close")
  }
}
