// Outbound-only worker bridge for the Django coordinator (issue #16).
// Adapted from the dashboard bridge `src/worker.mjs`; the sync envelope and
// response follow the versioned Django contract (`POST /api/worker/sync`,
// protocol_version 1).
import { hostname } from "node:os"
import { pathToFileURL } from "node:url"
import { setTimeout } from "node:timers/promises"
import { randomUUID } from "node:crypto"
import { OpenCodeConnection, consumeSSE } from "./opencode.mjs"
import { CommandReceipts } from "./receipts.mjs"
import { workerSettings, workerOptions } from "./settings.mjs"
import { LiveSnapshots, syncScheduler } from "./live.mjs"

const EVENT_BUFFER = 1000

export function startWorker(input) {
  const options = workerSettings(input)
  const controller = new AbortController()
  const connection = new OpenCodeConnection({ ...options, signal: controller.signal })
  const receipts = new CommandReceipts(options.stateDirectory)
  const workerID = options.worker
  const sessions = new Map()
  const scheduler = syncScheduler(controller.signal)
  const live = new LiveSnapshots({ coordinatorURL: options.coordinator, worker: workerID,
    key: options.key, signal: controller.signal, connection, sessions,
    messageLimit: options.messageLimit, wake: scheduler.wake })
  const messages = new Map()
  const queue = []
  const results = []
  const seenDirectories = new Set()
  let streamConnected = false
  let dropped = 0
  let snapshotEpoch
  let models = []
  let agents = []
  let catalogAt = 0
  let eventCursor = 0
  let inFlight = null
  let localConnected = false
  let localError
  let pending = { permissions: [], questions: [] }
  const enqueue = (event) => {
    // Intermediate text/tool parts are replaceable live state, not durable events.
    if (["message.part.delta", "message.part.updated", "message.updated"].includes(event.type)) return
    queue.push(event)
    if (queue.length > EVENT_BUFFER) {
      queue.shift()
      dropped++
    }
  }

  async function stream() {
    while (!controller.signal.aborted) {
      try {
        const response = await fetch(connection.endpoint("/global/event", undefined), {
          headers: connection.headers,
          signal: controller.signal,
        })
        if (!response.ok || !response.headers.get("content-type")?.includes("text/event-stream")) {
          throw new Error("SSE unavailable")
        }
        streamConnected = true
        await consumeSSE(response.body, (frame) => {
          const event = frame.payload ?? frame
          if (!event.type || event.type === "server.heartbeat") return
          // Local directory filtering is applied to snapshots too.
          if (options.directory && frame.directory && frame.directory !== options.directory) return
          // Pending interactions are workspace-scoped, so remember workspaces
          // that raised one even when their session falls outside the window.
          if (frame.directory && typeof event.type === "string" &&
            (event.type.startsWith("permission.") || event.type.startsWith("question."))) {
            seenDirectories.add(frame.directory)
            if (seenDirectories.size > 50) seenDirectories.delete(seenDirectories.values().next().value)
          }
          const sessionID = event.properties?.sessionID ?? event.properties?.part?.sessionID ??
            event.properties?.info?.sessionID ?? (event.type.startsWith("session.") ? event.properties?.info?.id : undefined)
          live.observe(event, sessionID)
          enqueue({
            type: event.type,
            sessionID:
              event.properties?.sessionID ??
              event.properties?.part?.sessionID ??
              event.properties?.info?.sessionID ??
              (event.type.startsWith("session.") ? event.properties?.info?.id : undefined),
             payload: frame,
          })
        })
      } catch (error) {
        if (!controller.signal.aborted) console.error(`[${workerID}] Event stream: ${error.message}`)
      } finally {
        streamConnected = false
      }
      await setTimeout(2000, undefined, { signal: controller.signal }).catch(() => {})
    }
  }

  async function tick() {
    if (inFlight) return deliver()
    const updates = []
    let connected = false
    let error
    let version
    try {
      const health = await connection.request("/global/health")
      version = health?.version
      if (version !== "1.18.32") {
        throw new Error("Unsupported OpenCode API: verified version is 1.18.32")
      }
      if (Date.now() - catalogAt > 60000) {
        const catalog = await connection.request("/provider")
        models = catalog.all
          .filter((provider) => catalog.connected.includes(provider.id))
          .flatMap((provider) =>
            Object.entries(provider.models).map(([id, model]) => ({
              providerID: provider.id,
              modelID: id,
              name: model.name ?? id,
            })),
          )
        try {
          const list = await connection.request("/agent")
          if (Array.isArray(list)) {
            agents = list
              .filter((agent) => agent && typeof agent.name === "string" && agent.name)
              .map((agent) => ({
                name: agent.name,
                description: agent.description ?? "",
                mode: agent.mode,
                builtIn: agent.builtIn === true,
              }))
          }
        } catch {
          // Agent discovery is optional; task prompts still use the worker default.
          agents = []
        }
        catalogAt = Date.now()
      }
      // The regular list is project-scoped; the v1 global list spans projects.
      const list = await connection.request(
        `${options.directory ? "/session" : "/experimental/session"}?limit=${options.sessionLimit}`,
      )
      const statuses = await connection.request("/session/status")
      if (!Array.isArray(list)) throw new Error("Unsupported session list response; expected OpenCode v1 API")
       const filtered = (options.directory ? list.filter((s) => s.directory === options.directory) : list).slice(0, options.sessionLimit)
      // Pending interactions are directory-scoped on the local API: a query
      // without a workspace never returns requests raised inside it. Merge the
      // scopes of every observed workspace (recent sessions plus event hints).
      const pendingPermissions = []
      const pendingQuestions = []
      for (const directory of new Set([...filtered.map((s) => s.directory), ...seenDirectories])) {
        if (!directory) continue
        const [scopedPermissions, scopedQuestions] = await Promise.all([
          connection.request("/permission", { directory }),
          connection.request("/question", { directory }),
        ])
        if (!Array.isArray(scopedPermissions) || !Array.isArray(scopedQuestions)) {
          throw new Error("Unexpected pending interaction response")
        }
        pendingPermissions.push(...scopedPermissions)
        pendingQuestions.push(...scopedQuestions)
      }
      const uniqueRequests = (requests) => [...new Map(requests.map((request) => [request.id, request])).values()]
      const permissions = uniqueRequests(pendingPermissions)
      const questions = uniqueRequests(pendingQuestions)
      // Requests can belong to an older session outside the recent-history window;
      // preserve them by observing their rows too (server retention keeps them).
      for (const id of new Set([...permissions, ...questions].map((r) => r.sessionID))) {
        if (filtered.some((s) => s.id === id)) continue
        const session = await connection.request(`/session/${encodeURIComponent(id)}`)
         if ((!options.directory || session.directory === options.directory) && filtered.length < 100) filtered.push(session)
      }
       pending = {
         // A truncated window must never resolve requests omitted by its cap.
         complete: permissions.length <= 200 && questions.length <= 200 &&
           [...permissions, ...questions].every((r) => filtered.some((s) => s.id === r.sessionID)),
         permissions: permissions.filter((r) => filtered.some((s) => s.id === r.sessionID)).slice(0, 200),
         questions: questions.filter((r) => filtered.some((s) => s.id === r.sessionID)).slice(0, 200),
      }
      for (const id of sessions.keys()) {
        if (!filtered.some((s) => s.id === id)) {
          sessions.delete(id)
          messages.delete(id)
        }
      }
      for (const [index, session] of filtered.entries()) {
        const previous = sessions.get(session.id)
        const info = { ...session, status: statuses[session.id]?.type ?? "idle" }
        sessions.set(session.id, info)
        // Periodically reconcile recent messages: embedded TUI processes may
        // share the DB without publishing their live stream on this endpoint.
        if (!previous || index < 8 || previous.time.updated !== session.time.updated) {
          const history = await connection.request(
            `/session/${encodeURIComponent(session.id)}/message?limit=${options.messageLimit}`,
            { directory: session.directory },
          )
          if (!Array.isArray(history)) throw new Error("Unexpected message history response")
          const serialized = JSON.stringify(history)
          if (messages.get(session.id) !== serialized) {
            const prior = messages.get(session.id)
            messages.set(session.id, serialized)
            updates.push({ sessionID: session.id, messages: history })
            live.mark(session.id)
            enqueue({
              type: prior ? "session.snapshot.changed" : "session.discovered",
              sessionID: session.id,
               payload: { title: session.title, messageCount: history.length, status: info.status },
            })
          }
        }
      }
      connected = true
     } catch (cause) {
       error = cause.message
       pending = { ...pending, complete: false }
    }
     localConnected = connected
     localError = error
     // Freeze every upload until its exact ID is acknowledged. New SSE frames
     // stay in the bounded queue instead of being mixed into a retried batch.
     const completed = results.splice(0, 200)
     const batchID = randomUUID()
     inFlight = {
       id: batchID,
       completed,
       body: JSON.stringify({
          worker: workerID,
          protocol_version: 1,
          meta: {
            hostname: hostname(),
            opencode_url: options.url,
            protocol_version: 1,
            opencode_version: version,
            directory: options.directory ?? "",
            create_directory: options.createDirectory ?? options.directory ?? "",
            connected,
            stream_connected: streamConnected,
            error,
            models,
            agents,
            default_model: options.model,
            session_limit: options.sessionLimit,
            message_limit: options.messageLimit,
            dropped_events: dropped,
          },
          sessions: [...sessions.values()],
          pending,
          updates,
           events: queue.splice(0, EVENT_BUFFER),
          results: completed,
          cursor: { after: eventCursor, batch_id: batchID },
         }),
     }
     await deliver()
   }

   async function deliver() {
     try {
       const response = await fetch(options.syncURL, {
         method: "POST",
         headers: { "Content-Type": "application/json", Authorization: `Bearer ${options.key}` },
         signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
         redirect: "error",
         body: inFlight.body,
       })
       if (!response.ok) throw new Error(`Coordinator HTTP ${response.status}`)
       const payload = await response.json()
       const commands = payload?.commands
       if (payload?.protocol_version !== 1 || payload.acknowledged_batch_id !== inFlight.id ||
         typeof payload.snapshot_epoch !== "string" || !Array.isArray(commands) || commands.length > 200 || !commands.every((command) =>
         command && typeof command.id === "string" && /^\d+$/.test(command.id) && command.worker === workerID &&
         ["create", "prompt", "interrupt", "permission.reply", "question.reply", "question.reject"].includes(command.action))) {
         throw new Error("Unsupported sync response or mismatched acknowledgement/worker")
       }
       await receipts.acknowledge(inFlight.completed)
       if (payload.live?.protocol_version === 1) live.enable()
      // The server assigns durable snapshot epochs; republish full snapshots
      // after its epoch changes (server restart or `resync_workers`).
       if (payload.resync_required || (snapshotEpoch && snapshotEpoch !== payload.snapshot_epoch)) messages.clear()
       snapshotEpoch = payload.snapshot_epoch
       eventCursor = payload.event_sequence ?? eventCursor
       inFlight = null
      for (const command of commands) {
        if (controller.signal.aborted) break
        results.push(
          await receipts.execute(command, async () => {
             if (!localConnected) throw new Error(localError ?? "Local OpenCode is unavailable")
            return connection.execute(command, [...sessions.values()], pending)
          }),
        )
      }
      if (commands.length) scheduler.wake()
    } catch (cause) {
       if (!controller.signal.aborted) console.error(`[${workerID}] Sync: ${cause.message}`)
    }
  }

  const done = (async () => {
    results.push(...(await receipts.pending()))
    const streaming = stream()
    console.log(`[${workerID}] ${options.worker} @ ${options.url} -> ${options.syncURL}`)
    while (!controller.signal.aborted) {
      await tick()
      await scheduler.wait(options.interval)
    }
    await streaming
  })()
  return { workerID, done, stop: () => controller.abort() }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const worker = startWorker(workerOptions())
  for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, worker.stop)
  await worker.done
}
