import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { CodexConnection } from "./codex.mjs"
import { CommandReceipts } from "./receipts.mjs"

test("Codex projects are scoped and approval decisions bind to the requesting session", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "codex-worker-"))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const options = { workspaceRoot: root, stateDirectory: join(root, "state"), sessionLimit: 10, messageLimit: 20 }
  const conn = new CodexConnection(options)
  const sent = []
  const calls = []
  conn.rpc = {
    send: (frame) => sent.push(frame),
    call: async (method, params) => {
      calls.push({ method, params })
      switch (method) {
        case "thread/start": return { thread: { id: "thread-a" } }
        case "turn/start": return { turn: { id: "turn-a" } }
        case "turn/interrupt": return {}
        case "thread/read": return { thread: { turns: [{ items: [{ type: "agentMessage", id: "a", text: "Hi" }] }] } }
        case "thread/resume": return {}
      }
      assert.fail(`Unexpected RPC: ${method}`)
    },
  }
  await assert.rejects(conn.execute({ action: "create", payload: { directory: "/" } }, []), /outside/)
  const session = await conn.execute({ action: "create", payload: { title: "Test" } }, [])
  const other = await conn.execute({ action: "create" }, [])
  const sessions = [session, other]
  await conn.execute({ action: "prompt", sessionID: session.id, payload: { text: "Hi" } }, sessions)
  const emit = () => {}
  conn.observe({ method: "turn/started", params: { threadId: "thread-a", turn: { id: "turn-a" } } }, emit)
  conn.observe({ method: "item/started", params: { threadId: "thread-a", item: { type: "agentMessage", id: "a", text: "" } } }, emit)
  conn.observe({ method: "item/agentMessage/delta", params: { threadId: "thread-a", itemId: "a", delta: "Hi" } }, emit)
  assert.equal((await conn.messages(session, 20))[0].parts[0].text, "Hi")
  assert.equal(conn.snapshot().sessions.find(({ id }) => id === session.id).status, "busy")

  conn.observe({ id: 7, method: "item/commandExecution/requestApproval",
    params: { threadId: "thread-a", command: "pwd", availableDecisions: ["accept", "decline"] } }, emit)
  const [approval] = conn.snapshot().pending.permissions
  assert.equal(approval.sessionID, session.id)
  const reply = (sessionID, choice) => conn.execute({
    action: "permission.reply", sessionID, payload: { requestID: approval.id, reply: choice },
  }, sessions)
  await assert.rejects(reply(other.id, "once"), /no longer pending/)
  await assert.rejects(reply(session.id, "always"), /unavailable/)
  await reply(session.id, "once")
  assert.deepEqual(sent.pop(), { id: 7, result: { decision: "accept" } })
  conn.observe({ id: 8, method: "item/commandExecution/requestApproval",
    params: { threadId: "thread-a", command: "pwd", availableDecisions: ["accept", "cancel"] } }, emit)
  const [cancelable] = conn.snapshot().pending.permissions
  await conn.execute({ action: "permission.reply", sessionID: session.id,
    payload: { requestID: cancelable.id, reply: "reject" } }, sessions)
  assert.deepEqual(sent.pop(), { id: 8, result: { decision: "cancel" } })
  await conn.execute({ action: "interrupt", sessionID: session.id }, sessions)
  assert.deepEqual(calls.at(-1), { method: "turn/interrupt", params: { threadId: "thread-a", turnId: "turn-a" } })
  conn.observe({ method: "turn/completed", params: { threadId: "thread-a", turn: { status: "interrupted" } } }, emit)

  assert.deepEqual(await new CommandReceipts(options.stateDirectory).pending(), [])
  const restarted = new CodexConnection(options)
  restarted.rpc = conn.rpc
  assert.equal((await restarted.messages(session, 20))[0].parts[0].text, "Hi")
  await restarted.execute({ action: "prompt", sessionID: session.id, payload: { text: "Continue" } }, sessions)
  assert.equal(calls.at(-2).method, "thread/resume")
  assert.equal(calls.at(-1).params.threadId, "thread-a")
})

test("Codex usage samples keep both windows and survive a quota RPC failure", async () => {
  const conn = new CodexConnection({ stateDirectory: tmpdir() })
  const rpc = { async call(method) {
    assert.equal(method, "account/rateLimits/read")
    return { rateLimitsByLimitId: { codex: {
      primary: { usedPercent: 25, windowDurationMins: 300, resetsAt: 1800000000 },
      secondary: { usedPercent: 100, windowDurationMins: 10080 },
      credits: { balance: "private" },
    } } }
  } }
  conn.rpc = rpc
  await conn.refreshUsage(rpc)
  assert.deepEqual(conn.usage.windows, [
    { remaining_percent: 75, duration_minutes: 300, resets_at: 1800000000 },
    { remaining_percent: 0, duration_minutes: 10080, resets_at: null },
  ])
  const lastSample = conn.usage
  rpc.call = async () => { throw new Error("Quota temporarily unavailable") }
  await conn.refreshUsage(rpc)
  assert.equal(conn.usage, lastSample)
})
