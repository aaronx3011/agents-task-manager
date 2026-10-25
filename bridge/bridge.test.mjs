import assert from "node:assert/strict"
import { createServer } from "node:http"
import { mkdtemp, realpath, rm, symlink } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { spawnSync } from "node:child_process"
import { setTimeout } from "node:timers/promises"
import test from "node:test"
import { OpenCodeConnection, consumeSSE } from "./opencode.mjs"
import { workerSettings } from "./settings.mjs"
import { CommandReceipts } from "./receipts.mjs"
import { startWorker } from "./worker.mjs"

const fixtures = new WeakMap()

function resources(t) {
  if (fixtures.has(t)) return fixtures.get(t)
  const owned = { handles: [], servers: [], directories: [] }
  fixtures.set(t, owned)
  t.after(async () => {
    // Stop receipt writers before deleting state. Separate FIFO after-hooks
    // raced a sync acknowledgement against rm and leaked the worker on failure.
    owned.handles.forEach((handle) => handle.stop())
    await Promise.allSettled(owned.handles.map((handle) => handle.done))
    for (const server of owned.servers) {
      server.closeAllConnections()
      await new Promise((resolve) => server.close(resolve))
    }
    await Promise.all(owned.directories.map((directory) => rm(directory, { recursive: true, force: true })))
  })
  return owned
}

async function serve(t, handle) {
  const server = createServer(async (request, response) => {
    let body = ""
    for await (const chunk of request) body += chunk
    await handle(request, response, body)
  })
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  resources(t).servers.push(server)
  return `http://127.0.0.1:${server.address().port}`
}

function json(response, value) {
  response.writeHead(200, { "Content-Type": "application/json" })
  response.end(JSON.stringify(value))
}

async function until(probe) {
  const deadline = Date.now() + 5000
  while (Date.now() < deadline) {
    const value = await probe()
    if (value) return value
    await setTimeout(10)
  }
  assert.fail("timed out waiting for fixture observation")
}

async function state(t) {
  const directory = await mkdtemp(join(tmpdir(), "atm-bridge-test-"))
  resources(t).directories.push(directory)
  return directory
}

async function localServer(t, { version = "1.18.32" } = {}) {
  const calls = []
  const url = await serve(t, (request, response, body) => {
    const path = new URL(request.url, "http://fixture").pathname
    const directory = new URL(request.url, "http://fixture").searchParams.get("directory")
    calls.push({ path, method: request.method, body: body && JSON.parse(body), url: request.url, auth: request.headers.authorization })
    if (path === "/global/event") {
      response.writeHead(200, { "Content-Type": "text/event-stream" })
      response.write('data: {"payload":{"type":"session.updated","properties":{"info":{"id":"ses_fixture"}}}}\n\n')
      return
    }
    if (path === "/global/health") return json(response, { version })
    if (path === "/provider") return json(response, { connected: ["p"], all: [{ id: "p", models: { m: { name: "Model" } } }] })
    if (path === "/session/status") return json(response, {})
    if (["/session", "/experimental/session"].includes(path) && request.method === "GET") {
      return json(response, [{ id: "ses_fixture", directory: "/fixture", title: "Fixture", time: { updated: 1 } }])
    }
    if (path.endsWith("/message")) return json(response, [])
    // Pending interactions are workspace-scoped: the installed API returns an
    // empty list without the directory query, exactly like production.
    if (path === "/permission") return json(response, directory ? [{ id: "per_1", sessionID: "ses_fixture" }] : [])
    if (path === "/question") return json(response, directory ? [{ id: "que_1", sessionID: "ses_fixture" }, { id: "que_2", sessionID: "ses_fixture" }] : [])
    return json(response, path === "/session" ? { id: "ses_created" } : {})
  })
  return { url, calls }
}

function ack(envelope, commands = [], extra = {}) {
  return { protocol_version: 1, acknowledged_batch_id: envelope.cursor.batch_id, snapshot_epoch: "epoch-1", event_sequence: 1, commands, ...extra }
}

function command(id, action, payload = {}, extra = {}) {
  return { id, worker: "fixture", action, sessionID: "ses_fixture", payload, ...extra }
}

async function worker(t, local, coordinatorURL, stateDirectory) {
  const handle = startWorker({ worker: "fixture", key: "coordinator-secret", url: local.url, coordinatorURL, stateDirectory, interval: 20, password: "local-secret" })
  resources(t).handles.push(handle)
  return handle
}

test("all six mappings preserve directories/models, pending checks and local auth", async (t) => {
  const local = await localServer(t)
  const connection = new OpenCodeConnection({ url: local.url, password: "local-secret", createDirectory: "/create", model: { providerID: "p", modelID: "default" } })
  const sessions = [{ id: "ses_fixture", directory: "/fixture" }]
  await connection.execute(command("1", "create", { title: "New", model: { providerID: "p", modelID: "m" } }), sessions)
  await connection.execute(command("2", "prompt", { text: "Record", noReply: true }), sessions)
  await connection.execute(command("3", "interrupt"), sessions)
  await connection.execute(command("4", "permission.reply", { reply: "once" }, { requestID: "per_1" }), sessions)
  await connection.execute(command("5", "question.reply", { answers: [["yes"]] }, { requestID: "que_1" }), sessions)
  await connection.execute(command("6", "question.reject", {}, { requestID: "que_2" }), sessions)
  const posts = local.calls.filter((call) => call.method === "POST")
  assert.deepEqual(posts.map((call) => call.path), ["/session", "/session/ses_fixture/prompt_async", "/session/ses_fixture/abort", "/permission/per_1/reply", "/question/que_1/reply", "/question/que_2/reject"])
  assert.deepEqual(posts[0].body, { title: "New", model: { providerID: "p", id: "m" } })
  assert.equal(new URL(posts[0].url, local.url).searchParams.get("directory"), "/create")
  assert.deepEqual(posts[1].body, { parts: [{ type: "text", text: "Record" }], noReply: true, model: { providerID: "p", modelID: "default" } })
  assert.deepEqual(posts[3].body, { reply: "once" })
  assert.deepEqual(posts[4].body, { answers: [["yes"]] })
  assert.ok(posts.slice(1).every((call) => new URL(call.url, local.url).searchParams.get("directory") === "/fixture"))
  await assert.rejects(connection.execute(command("7", "question.reply", { answers: [] }, { requestID: "missing" }), sessions), /no longer pending/)
  assert.ok(local.calls.every((call) => call.auth === `Basic ${Buffer.from("opencode:local-secret").toString("base64")}`))
})

test("lost sync response retries identical body, retains outcomes and republishes on epoch change", async (t) => {
  const directory = await state(t)
  const local = await localServer(t)
  const uploads = []
  let lost
  let dispatch = true
  let resultsReceived = false
  let epochChanged = false
  let resynced = false
  const hub = await serve(t, (request, response, body) => {
    assert.equal(request.headers.authorization, "Bearer coordinator-secret")
    assert.ok(!body.includes("local-secret"))
    const envelope = JSON.parse(body)
    uploads.push(envelope)
    if (envelope.results.length && !lost) {
      lost = body
      response.destroy()
      return
    }
    if (envelope.results.length) {
      assert.equal(body, lost)
      resultsReceived = true
    }
    if (resultsReceived && !epochChanged) {
      epochChanged = true
      return json(response, ack(envelope, [], { snapshot_epoch: "epoch-2", resync_required: true }))
    }
    if (epochChanged && envelope.updates.length) resynced = true
    const commands = dispatch ? [command("1", "prompt", { text: "once", noReply: true })] : []
    dispatch = false
    json(response, ack(envelope, commands, { snapshot_epoch: epochChanged ? "epoch-2" : "epoch-1" }))
  })
  await worker(t, local, hub, directory)
  await until(() => resynced)
  assert.equal(local.calls.filter((call) => call.path.endsWith("/prompt_async")).length, 1)
  assert.ok(uploads.some((upload) => upload.events.some((event) => event.payload && !event.frame)))
  const receipts = new CommandReceipts(directory)
  await until(() => resultsReceived)
  await until(async () => (await receipts.pending()).length === 0)
  assert.deepEqual(await receipts.pending(), [])
})

test("crash after write-ahead intent publishes uncertain outcome without executing prompt", async (t) => {
  const directory = await state(t)
  const receiptModule = new URL("./receipts.mjs", import.meta.url).href
  const crash = spawnSync(process.execPath, ["--input-type=module", "-e", `
    import { CommandReceipts } from ${JSON.stringify(receiptModule)};
    await new CommandReceipts(${JSON.stringify(directory)}).execute(${JSON.stringify(command("9", "prompt", { text: "never replay" }))}, async () => process.exit(0));
  `])
  assert.equal(crash.status, 0, crash.stderr.toString())
  const local = await localServer(t)
  let reported
  const hub = await serve(t, (request, response, body) => {
    const envelope = JSON.parse(body)
    reported = envelope.results[0] ?? reported
    json(response, ack(envelope))
  })
  await worker(t, local, hub, directory)
  await until(() => reported)
  assert.equal(reported.id, "9")
  assert.equal(reported.status, "error")
  assert.match(reported.error, /uncertain.*Not replayed/)
  assert.equal(local.calls.filter((call) => call.method === "POST").length, 0)
})

test("pending interactions travel through sync, dispatch and local replies", async (t) => {
  const local = await localServer(t)
  const results = []
  let dispatched = false
  const hub = await serve(t, (request, response, body) => {
    const envelope = JSON.parse(body)
    assert.equal(envelope.pending.complete, true)
    assert.equal(envelope.pending.permissions[0].id, "per_1")
    assert.equal(envelope.pending.questions.length, 2)
    results.push(...envelope.results)
    const commands = dispatched ? [] : [
      command("20", "permission.reply", { reply: "once" }, { requestID: "per_1" }),
      command("21", "question.reply", { answers: [["yes"]] }, { requestID: "que_1" }),
      command("22", "question.reject", {}, { requestID: "que_2" }),
    ]
    dispatched = true
    json(response, ack(envelope, commands))
  })
  await worker(t, local, hub, await state(t))
  await until(() => results.length === 3)
  assert.ok(results.every((result) => result.status === "done"))
  const pendingLists = local.calls.filter((call) => call.method === "GET" && ["/permission", "/question"].includes(call.path))
  assert.ok(pendingLists.length > 0)
  assert.ok(pendingLists.every((call) => new URL(call.url, local.url).searchParams.get("directory") === "/fixture"))
  assert.deepEqual(local.calls.filter((call) => call.method === "POST").map((call) => call.path), [
    "/permission/per_1/reply", "/question/que_1/reply", "/question/que_2/reject",
  ])
})

test("durable completed receipts suppress redelivery and reject changed payloads", async (t) => {
  const directory = await state(t)
  let executions = 0
  const input = command("30", "prompt", { text: "only once", noReply: true })
  const execute = async () => { executions++; return { accepted: true } }
  const original = await new CommandReceipts(directory).execute(input, execute)
  const reopened = new CommandReceipts(directory)
  assert.deepEqual(await reopened.pending(), [original])
  assert.deepEqual(await reopened.execute(input, execute), original)
  const changed = await reopened.execute({ ...input, payload: { text: "changed" } }, execute)
  assert.equal(changed.status, "error")
  assert.match(changed.error, /uncertain/)
  assert.equal(executions, 1)
  await reopened.acknowledge([original])
  assert.deepEqual(await new CommandReceipts(directory).pending(), [])
})

test("unsupported installed version refuses local command execution", async (t) => {
  const local = await localServer(t, { version: "2.0.0" })
  let result
  let sent = false
  const hub = await serve(t, (request, response, body) => {
    const envelope = JSON.parse(body)
    assert.equal(envelope.meta.connected, false)
    assert.match(envelope.meta.error, /Unsupported OpenCode/)
    result = envelope.results[0] ?? result
    const commands = sent ? [] : [command("10", "create")]
    sent = true
    json(response, ack(envelope, commands))
  })
  await worker(t, local, hub, await state(t))
  await until(() => result)
  assert.equal(result.status, "error")
  assert.equal(local.calls.filter((call) => call.method === "POST").length, 0)
})

test("wrong acknowledgement and foreign worker commands are rejected before execution", async (t) => {
  const local = await localServer(t)
  let attempts = 0
  const hub = await serve(t, (request, response, body) => {
    const envelope = JSON.parse(body)
    attempts++
    json(response, attempts === 1
      ? ack(envelope, [command("11", "create")], { acknowledged_batch_id: "wrong" })
      : ack(envelope, [command("11", "create", {}, { worker: "foreign" })]))
  })
  await worker(t, local, hub, await state(t))
  await until(() => attempts >= 3)
  assert.equal(local.calls.filter((call) => call.method === "POST").length, 0)
})

test("coordinator credentials cannot be redirected to a foreign sync origin", () => {
  const options = { worker: "fixture", key: "secret", coordinatorURL: "https://coordinator.example" }
  assert.throws(() => workerSettings({ ...options, syncPath: "https://foreign.example/sync" }), /origin/)
  assert.throws(() => workerSettings({ ...options, sessionLimit: 101 }), /window limit/)
  assert.throws(() => workerSettings({ ...options, messageLimit: 301 }), /window limit/)
})

test("workspace aliases are canonicalized to match OpenCode session directories", async (t) => {
  const directory = await state(t)
  const alias = join(directory, "workspace-alias")
  await symlink(directory, alias, "dir")
  const settings = workerSettings({ worker: "fixture", key: "secret", coordinatorURL: "https://coordinator.example", directory: alias })
  assert.equal(settings.directory, await realpath(directory))
})

test("SSE decoding preserves fragmented UTF-8 and CRLF frames", async () => {
  const bytes = new TextEncoder().encode('data: {"text":"héllo"}\r\n\r\n')
  const events = []
  await consumeSSE((async function* () {
    for (const byte of bytes) yield new Uint8Array([byte])
  })(), (event) => events.push(event))
  assert.deepEqual(events, [{ text: "héllo" }])
})
