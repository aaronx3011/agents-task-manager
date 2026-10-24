// Two-worker live smoke harness for the Django coordinator bridge (issue #16).
//
// Usage: node bridge/smoke.mjs  (Node 20+)
// Env:
//   COORDINATOR_URL        default http://127.0.0.1:8321
//   WORKER_A_ID/KEY        first provisioned worker (Django Worker.name + bound agent key)
//   WORKER_B_ID/KEY        second worker (both bound, e.g. to different agents)
//   OPENCODE_URL           default http://127.0.0.1:4096 (installed OpenCode 1.18.32)
//   SMOKE_DIRECTORY        workspace directory for created sessions
//
// The harness publishes both workers, creates ONE session through the command
// queue, submits a record-only prompt, confirms delivery/results and observed
// history, checks cross-worker isolation, and restarts worker A to prove a
// queued command is executed exactly once.
import { setTimeout } from "node:timers/promises"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { spawnSync } from "node:child_process"
import { startWorker } from "./worker.mjs"

const coordinator = process.env.COORDINATOR_URL ?? "http://127.0.0.1:8321"
const opencode = process.env.OPENCODE_URL ?? "http://127.0.0.1:4096"
const directory = process.env.SMOKE_DIRECTORY ?? `${process.env.HOME ?? "."}/open/worker-sandbox`
const workers = [
  { id: process.env.WORKER_A_ID, key: process.env.WORKER_A_KEY },
  { id: process.env.WORKER_B_ID, key: process.env.WORKER_B_KEY },
].map((option, index) => ({
  id: option.id ?? `release-driver-${index + 1}`,
  key: option.key,
  url: (index === 0 ? process.env.OPENCODE_A_URL : process.env.OPENCODE_B_URL) ?? opencode,
}))
if (workers.some((worker) => !worker.key)) {
  console.error("smoke: WORKER_A_KEY (and optionally WORKER_B_KEY/ID) must hold the bound agent keys")
  process.exit(2)
}
const stamp = `${Date.now()}`
const state = mkdtempSync(join(tmpdir(), "atm-smoke-"))
const running = []
const watchdog = globalThis.setTimeout(() => {
  console.error("smoke: exceeded five-minute deadline")
  running.forEach((handle) => handle.stop())
  process.exitCode = 1
}, 300000)
const model = process.env.SMOKE_MODEL

async function api(path, key, init) {
  const response = await fetch(`${coordinator}${path}`, {
    ...init,
    signal: AbortSignal.timeout(15000),
    redirect: "error",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
  })
  const text = await response.text()
  const body = text ? JSON.parse(text) : null
  if (!response.ok) throw new Error(`API ${path} -> ${response.status}: ${text.slice(0, 300)}`)
  return body
}

async function until(label, deadline, probe) {
  let last
  while (Date.now() < deadline) {
    try {
      last = await probe()
      if (last.ok) return last.value
    } catch (cause) {
      last = cause.message
    }
    await setTimeout(500)
  }
  throw new Error(`smoke: timed out waiting for ${label}: ${JSON.stringify(last)?.slice(0, 300)}`)
}

const note = (message) => console.log(`smoke: ${message}`)
const failures = []

async function expect(label, run) {
  try {
    await run()
    note(`ok ${label}`)
  } catch (cause) {
    failures.push(label)
    console.error(`smoke: FAIL ${label}: ${cause.message}`)
  }
}

try {
  // Start both bridges against the same local OpenCode, isolated state dirs.
  const handles = workers.map((worker, index) => {
    const handle = startWorker({
      worker: worker.id,
      key: worker.key,
      coordinatorURL: coordinator,
      url: worker.url,
      directory,
      stateDirectory: join(state, `worker-${index}`),
      sessionLimit: 50,
      messageLimit: 30,
      interval: 1000,
    })
    running.push(handle)
    return { ...worker, handle }
  })
  // 1. Credential-free observation: models + connection reach the read model.
  for (const worker of handles) {
    await expect(`worker ${worker.id} online with models`, async () => {
      const detail = await until(`${worker.id} online`, Date.now() + 45000, async () => {
        const data = await api(`/api/workers/${worker.id}`, worker.key)
        return data.online && data.connected && data.stream_connected && Array.isArray(data.models)
          ? { ok: true, value: data }
          : { ok: false }
      })
      if (!detail.models.some((model) => model.providerID && model.modelID)) {
        throw new Error("model catalog empty")
      }
    })
  }
  // 2. Command queue: create one session, wait for worker results + delivery.
  const command = await api("/api/commands", handles[0].key, {
    method: "POST",
    body: JSON.stringify({
      worker: handles[0].id,
      action: "create",
      payload: { directory, title: `Smoke ${stamp}` },
    }),
  })
  note(`create command ${command.id} queued`)
  const sessionID = await until("create command result", Date.now() + 45000, async () => {
    const card = await api(`/api/commands/${command.id}`, handles[0].key)
    return card.status === "done" ? { ok: true, value: card.result?.id } : card.status === "error" ? { ok: false, value: card.error } : { ok: false }
  })
  await until("created session observed", Date.now() + 45000, async () => {
    const read = await api(`/api/workers/${handles[0].id}/sessions`, handles[0].key)
    return read.items.some((session) => session.sessionID === sessionID) ? { ok: true } : { ok: false }
  })
  // 3. Record-only prompt: accepted, then observed message history; no reply.
  await expect(`prompt ${sessionID} delivered`, async () => {
    const prompt = await api("/api/commands", handles[0].key, {
      method: "POST",
      body: JSON.stringify({
        worker: handles[0].id,
        action: "prompt",
        sessionID,
        payload: { text: `smoke ${stamp}: record-only prompt`, noReply: true },
      }),
    })
    const card = await until("prompt result", Date.now() + 45000, async () => {
      const data = await api(`/api/commands/${prompt.id}`, handles[0].key)
      return ["done", "error"].includes(data.status) ? { ok: true, value: data } : { ok: false }
    })
    if (card.status === "error") throw new Error(card.error)
    const observed = await until("prompt observed", Date.now() + 45000, async () => {
      const read = await api(`/api/workers/${handles[0].id}/sessions/${sessionID}/messages?limit=500`, handles[0].key)
      return read.messages.some((message) => message.parts?.[0]?.text?.includes(`smoke ${stamp}`))
        ? { ok: true, value: read }
        : { ok: false }
    })
    if (!observed.messages.every((message) => message.content !== undefined)) {
      throw new Error("message cards must expose content")
    }
  })
  note(`observed session: ${sessionID}`)
  await expect("interrupt mapping accepted for idle smoke session", async () => {
    const command = await api("/api/commands", handles[0].key, {
      method: "POST",
      body: JSON.stringify({ worker: handles[0].id, action: "interrupt", sessionID, payload: {} }),
    })
    const card = await until("interrupt result", Date.now() + 45000, async () => {
      const data = await api(`/api/commands/${command.id}`, handles[0].key)
      return ["done", "error"].includes(data.status) ? { ok: true, value: data } : { ok: false }
    })
    if (card.status !== "done") throw new Error(card.error)
  })
  if (model) await expect(`live model completion (${model})`, async () => {
    const [providerID, ...rest] = model.split("/")
    if (!providerID || !rest.length) throw new Error("SMOKE_MODEL must be providerID/modelID")
    const marker = `SMOKE-MODEL-OK-${stamp}`
    const command = await api("/api/commands", handles[0].key, {
      method: "POST",
      body: JSON.stringify({ worker: handles[0].id, action: "prompt", sessionID,
        payload: { text: `This is a model connectivity smoke check. After any required read-only status checks, reply with exactly ${marker} and nothing else. Do not claim, create or change tasks, or change any files.`, model: { providerID, modelID: rest.join("/") } } }),
    })
    const acceptance = await until("live prompt acceptance", Date.now() + 45000, async () => {
      const card = await api(`/api/commands/${command.id}`, handles[0].key)
      return ["done", "error"].includes(card.status) ? { ok: true, value: card } : { ok: false }
    })
    if (acceptance.status !== "done") throw new Error(acceptance.error)
    note(`live prompt ${command.id} accepted; waiting for assistant completion`)
    try {
    const completion = await until("live model completion", Date.now() + 90000, async () => {
      const read = await api(`/api/workers/${handles[0].id}/sessions/${sessionID}/messages?limit=500`, handles[0].key)
      const user = read.messages.find((message) => message.role === "user" && message.parts?.some((part) => part.text?.includes(marker)))
      // Each tool step has its own completed timestamp. Wait for the final
      // assistant response, not an intermediate completed tool-calls step.
      const reply = user && read.messages.find((message) => message.role === "assistant" && message.content?.parentID === user.content.id &&
        (message.content.error || (message.content.time?.completed && typeof message.content.finish === "string" && message.content.finish !== "tool-calls")))
      return reply ? { ok: true, value: reply } : { ok: false }
    })
    if (completion.content.error) throw new Error(`OpenCode execution failed: ${completion.content.error.name}`)
    if (!completion.parts.some((part) => part.type === "text" && part.text?.includes(marker))) {
      throw new Error("assistant completed without the expected marker")
    }
    } finally {
      // Stop only the session this harness created, including a timed-out run.
      const abort = await api("/api/commands", handles[0].key, {
        method: "POST",
        body: JSON.stringify({ worker: handles[0].id, action: "interrupt", sessionID, payload: {} }),
      })
      await until("live model cleanup", Date.now() + 30000, async () => {
        const card = await api(`/api/commands/${abort.id}`, handles[0].key)
        return card.status === "done" ? { ok: true } : { ok: false }
      })
    }
  })
  // 4. Cross-worker isolation: same remote id, two worker-scoped rows.
  await expect("worker-scoped endpoint isolation", async () => {
    let secondSessionID = sessionID
    if (handles[0].url !== handles[1].url) {
      const second = await api("/api/commands", handles[1].key, {
        method: "POST",
        body: JSON.stringify({ worker: handles[1].id, action: "create", payload: { directory, title: `Smoke B ${stamp}` } }),
      })
      secondSessionID = await until("second endpoint create", Date.now() + 45000, async () => {
        const card = await api(`/api/commands/${second.id}`, handles[1].key)
        return card.status === "done" ? { ok: true, value: card.result?.id } : { ok: false }
      })
    }
    for (const worker of handles) {
      await until(`${worker.id} sees the session`, Date.now() + 45000, async () => {
        const listing = await api(`/api/workers/${worker.id}/sessions`, worker.key)
        const expected = worker.id === handles[0].id ? sessionID : secondSessionID
        return listing.items.some((item) => item.sessionID === expected) ? { ok: true } : { ok: false }
      })
    }
    // Each agent key's unified read shows only its own worker's row.
    for (const worker of handles) {
      const unified = await api("/api/sessions?limit=500", worker.key)
      const expected = worker.id === handles[0].id ? sessionID : secondSessionID
      const own = unified.items.filter((item) => item.sessionID === expected)
      if (own.length !== 1 || own[0].worker !== worker.id) {
        throw new Error(`expected exactly one worker-scoped row for ${worker.id}`)
      }
      if (secondSessionID !== sessionID && unified.items.some((item) => item.sessionID === (expected === sessionID ? secondSessionID : sessionID))) {
        throw new Error(`foreign endpoint session visible to ${worker.id}`)
      }
    }
  })
  // 5. Restart worker A while a command is queued: delivery exactly once.
  await expect("restart executes queued prompt exactly once", async () => {
    note("stopping worker A")
    const offline = handles[0]
     offline.handle.stop()
     await offline.handle.done
    const prompt = await api("/api/commands", offline.key, {
      method: "POST",
      body: JSON.stringify({
        worker: offline.id,
        action: "prompt",
        sessionID,
        payload: { text: `smoke ${stamp}: restart exactly-once`, noReply: true },
      }),
    })
    // Restart the SAME worker id; the receipt dir is fresh, but the queue is
    // durable server-side so it must be delivered once (never replayed).
     const restarted = startWorker({
      worker: offline.id,
      key: offline.key,
      coordinatorURL: coordinator,
      url: offline.url,
      directory,
      stateDirectory: join(state, "worker-0"),
      sessionLimit: 50,
      messageLimit: 30,
      interval: 1000,
     })
     running.push(restarted)
    const card = await until("restarted prompt result", Date.now() + 90000, async () => {
      const data = await api(`/api/commands/${prompt.id}`, offline.key)
      return ["done", "error"].includes(data.status) ? { ok: true, value: data } : { ok: false }
    })
    if (card.status === "error") throw new Error(card.error)
    const messages = await until("restart prompt observed once", Date.now() + 90000, async () => {
      const read = await api(`/api/workers/${offline.id}/sessions/${sessionID}/messages?limit=500`, offline.key)
      const matches = read.messages.filter((message) => message.parts?.[0]?.text?.includes(`${stamp}: restart exactly-once`))
      return matches.length >= 1 ? { ok: true, value: matches.length } : { ok: false }
    })
    if (messages !== 1) throw new Error(`published ${messages} copies, expected exactly 1`)
    restarted.stop()
    await restarted.done
  })
  await expect("interrupted intent becomes uncertain without replay", async () => {
    const worker = handles[0]
    const marker = `smoke ${stamp}: crash-before-call`
    const queued = await api("/api/commands", worker.key, {
      method: "POST",
      body: JSON.stringify({ worker: worker.id, action: "prompt", sessionID, payload: { text: marker, noReply: true } }),
    })
    // Explicitly receive the dispatch while the bridge is stopped, then crash
    // the real receipt writer immediately after its durable intent is fsynced.
    const dispatch = await api("/api/worker/sync", worker.key, {
      method: "POST",
      body: JSON.stringify({ worker: worker.id, protocol_version: 1, meta: {} }),
    })
    const command = dispatch.commands.find((command) => command.id === queued.id)
    if (!command) throw new Error("crash fixture did not receive its command")
    const crash = spawnSync(process.execPath, ["--input-type=module", "-e", `
      import { CommandReceipts } from ${JSON.stringify(new URL("./receipts.mjs", import.meta.url).href)};
      await new CommandReceipts(${JSON.stringify(join(state, "worker-0"))}).execute(${JSON.stringify(command)}, async () => process.exit(0));
    `])
    if (crash.status !== 0) throw new Error("crash fixture failed to persist its intent")
    const recovered = startWorker({ worker: worker.id, key: worker.key, coordinatorURL: coordinator,
      url: worker.url, directory, stateDirectory: join(state, "worker-0"), interval: 1000 })
    running.push(recovered)
    await until("uncertain command published", Date.now() + 45000, async () => {
      const card = await api(`/api/commands/${command.id}`, worker.key)
      return card.deliveryUncertain && card.status === "dispatched" ? { ok: true } : { ok: false }
    })
    await setTimeout(2200)
    const read = await api(`/api/workers/${worker.id}/sessions/${sessionID}/messages?limit=500`, worker.key)
    if (read.messages.some((message) => message.parts?.some((part) => part.text === marker))) {
      throw new Error("interrupted intent was replayed")
    }
    note(`command ${command.id} remains explicitly uncertain for staff reconciliation`)
  })
   if (failures.length > 0) {
     console.error(`smoke: FAILED (${failures.length}): ${failures.join("; ")}`)
     process.exitCode = 1
  } else {
    note(`OK: session ${sessionID}, two workers isolated, restart executed exactly once`)
   }
 } finally {
   running.forEach((handle) => handle.stop())
   await Promise.allSettled(running.map((handle) => handle.done))
   globalThis.clearTimeout(watchdog)
   rmSync(state, { recursive: true, force: true })
 }
