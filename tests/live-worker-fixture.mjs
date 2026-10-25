// Real Node bridge + local HTTP/SSE fixture for test_live_network.py. No model calls.
import { createServer } from "node:http"
import { createInterface } from "node:readline"

const { startWorker } = await import(process.env.TEST_BRIDGE_MODULE)
const streams = new Set()
let text = ""
let requests = 0
let prompts = 0
const server = createServer((request, response) => {
  const path = new URL(request.url, "http://fixture").pathname
  const json = (value) => {
    response.writeHead(200, { "Content-Type": "application/json" })
    response.end(JSON.stringify(value))
  }
  if (path.endsWith("/prompt_async")) { prompts++; return json({ accepted: true }) }
  if (path === "/global/event") {
    response.writeHead(200, { "Content-Type": "text/event-stream" })
    response.write(": connected\n\n")
    streams.add(response)
    request.on("close", () => streams.delete(response))
    return
  }
  if (path === "/global/health") return json({ version: "1.18.32" })
  if (path === "/provider") return json({ connected: [], all: [] })
  if (path === "/session/status") return json({ ses_shared: { type: text ? "busy" : "idle" } })
  if (path === "/session" || path === "/experimental/session") {
    return json([{ id: "ses_shared", directory: "/fixture", title: "Live test", time: { updated: 1 } }])
  }
  if (path.endsWith("/message")) {
    requests++
    return json(text ? [{ info: { id: "msg_live", sessionID: "ses_shared", role: "assistant" },
      parts: [{ type: "text", text }] }] : [])
  }
  return json([])
})
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
const key = process.env.TEST_WORKER_KEY
const worker = startWorker({
  worker: "worker-a", id: "worker-a", key, coordinatorKey: key,
  coordinatorURL: process.env.TEST_COORDINATOR_URL,
  url: `http://127.0.0.1:${server.address().port}`, interval: 9000,
  stateDirectory: process.env.TEST_WORKER_STATE,
})
const input = createInterface({ input: process.stdin })
input.on("line", (line) => {
  const command = JSON.parse(line)
  if (command.text) {
    text = command.text
    for (let i = 0; i < 100; i++) {
      for (const stream of streams) stream.write(`data: ${JSON.stringify({ payload: {
        type: "message.part.delta", properties: { sessionID: "ses_shared", delta: "x" },
      } })}\n\n`)
    }
  }
  if (command.finish) {
    for (const stream of streams) stream.write(`data: ${JSON.stringify({ payload: {
      type: "session.status", properties: { sessionID: "ses_shared", status: { type: "idle" } },
    } })}\n\n`)
  }
  if (command.stop) { worker.stop(); input.close() }
})
console.log("FIXTURE_READY")
try { await worker.done } finally {
  for (const stream of streams) stream.end()
  server.closeAllConnections()
  await new Promise((resolve) => server.close(resolve))
  console.log(`FIXTURE_HISTORY_REQUESTS=${requests}`)
  console.log(`FIXTURE_PROMPT_CALLS=${prompts}`)
}
