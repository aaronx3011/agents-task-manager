import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { ClaudeConnection, usageFromRateLimit } from "./claude.mjs"

// A fake Agent SDK: each query records its options and exposes the prompts
// pulled from the input stream; the test pushes SDK messages back out.
function fakeSDK() {
  const queries = []
  const sdk = {
    queries,
    account: { apiKeySource: "ANTHROPIC_API_KEY", tokenSource: "none", apiProvider: "firstParty" },
    query({ prompt, options }) {
      const outbox = []
      const waiters = []
      const q = {
        options, prompts: [], interrupted: 0, model: undefined,
        emit(message) {
          const waiter = waiters.shift()
          if (waiter) waiter({ value: message, done: false })
          else outbox.push(message)
        },
        end() {
          for (const waiter of waiters.splice(0)) waiter({ value: undefined, done: true })
          q.ended = true
        },
        interrupt: async () => { q.interrupted++ },
        setModel: async (model) => { q.model = model },
        supportedModels: async () => [{ value: "sonnet", displayName: "Sonnet" }],
        accountInfo: async () => sdk.account,
        close() { q.end() },
        [Symbol.asyncIterator]() {
          return { next: () => outbox.length ? Promise.resolve({ value: outbox.shift(), done: false })
            : q.ended ? Promise.resolve({ value: undefined, done: true })
              : new Promise((resolve) => waiters.push(resolve)) }
        },
      }
      ;(async () => { for await (const item of prompt) q.prompts.push(item) })()
      queries.push(q)
      return q
    },
    getSessionMessages: async () => [
      { type: "user", uuid: "u1", message: { role: "user", content: "Hi" }, parent_tool_use_id: null },
      { type: "assistant", uuid: "a1", message: { id: "msg_1", content: [{ type: "text", text: "Hello" }] }, parent_tool_use_id: null },
    ],
  }
  return sdk
}

const tick = () => new Promise((resolve) => setImmediate(resolve))

test("Claude sessions stream, gate tools on the dashboard and resume after restart", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "claude-worker-"))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const sdk = fakeSDK()
  const controller = new AbortController()
  const options = { workspaceRoot: root, stateDirectory: join(root, "state"), sessionLimit: 10,
    messageLimit: 20, claudeSDK: sdk, signal: controller.signal }
  const conn = new ClaudeConnection(options)
  const frames = []
  let ready = false
  const running = conn.stream((frame) => frames.push(frame), () => { ready = true })
  await tick()
  assert.ok(ready)
  assert.deepEqual(await conn.catalog(), [{ providerID: "anthropic", modelID: "sonnet", name: "Sonnet" }])
  assert.equal(sdk.queries[0].options.persistSession, false, "the sign-in probe never persists a session")

  await assert.rejects(conn.execute({ action: "create", payload: { directory: "/" } }, []), /outside/)
  await assert.rejects(conn.execute({ action: "create", payload: { directory: "/User/missing" } }, []),
    /Working directory does not exist on this worker: \/User\/missing/)
  const session = await conn.execute({ action: "create", payload: { title: "Test",
    model: { providerID: "anthropic", modelID: "sonnet" } } }, [])
  const other = await conn.execute({ action: "create" }, [])
  const sessions = [session, other]
  await assert.rejects(conn.execute({ action: "interrupt", sessionID: session.id }, sessions), /No Claude turn/)

  await conn.execute({ action: "prompt", sessionID: session.id, payload: { text: "Hi" } }, sessions)
  await tick()
  const q = sdk.queries[1]
  assert.equal(q.options.cwd, conn.directory(root))
  assert.equal(q.options.model, "sonnet")
  assert.equal(q.options.resume, undefined)
  assert.equal(q.prompts[0].message.content, "Hi")
  assert.equal(conn.snapshot().sessions.find(({ id }) => id === session.id).status, "busy")

  // Streamed deltas and the final block share one dashboard row.
  q.emit({ type: "stream_event", session_id: "claude-1", parent_tool_use_id: null, event: { type: "message_start", message: { id: "msg_a" } } })
  q.emit({ type: "stream_event", session_id: "claude-1", parent_tool_use_id: null, event: { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } } })
  q.emit({ type: "stream_event", session_id: "claude-1", parent_tool_use_id: null, event: { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Hel" } } })
  await tick()
  let history = await conn.messages(session, 20)
  assert.equal(history.at(-1).parts[0].text, "Hel")
  q.emit({ type: "assistant", session_id: "claude-1", uuid: "x", parent_tool_use_id: null, message: { id: "msg_a", content: [{ type: "text", text: "Hello" }] } })
  q.emit({ type: "assistant", session_id: "claude-1", uuid: "y", parent_tool_use_id: null, message: { id: "msg_a", content: [{ type: "tool_use", id: "tu_1", name: "Bash", input: { command: "pwd" } }] } })
  await tick()
  history = await conn.messages(session, 20)
  assert.deepEqual(history.map(({ parts }) => parts[0].type), ["text", "text", "tool"])
  assert.equal(history[1].parts[0].text, "Hello")

  // Tool approval: bound to the requesting session; "always" forwards suggestions.
  const suggestions = [{ type: "addRules", rules: [{ toolName: "Bash" }], behavior: "allow", destination: "session" }]
  const decision = q.options.canUseTool("Bash", { command: "pwd" }, { signal: new AbortController().signal, suggestions })
  const [approval] = conn.snapshot().pending.permissions
  assert.equal(approval.sessionID, session.id)
  assert.deepEqual(approval.patterns, ["pwd"])
  const reply = (sessionID, choice) => conn.execute({ action: "permission.reply", sessionID,
    payload: { requestID: approval.id, reply: choice } }, sessions)
  await assert.rejects(reply(other.id, "once"), /no longer pending/)
  await reply(session.id, "always")
  assert.deepEqual(await decision, { behavior: "allow", updatedInput: { command: "pwd" }, updatedPermissions: suggestions })
  assert.equal(conn.snapshot().pending.permissions.length, 0)
  q.emit({ type: "user", session_id: "claude-1", parent_tool_use_id: null, message: { role: "user",
    content: [{ type: "tool_result", tool_use_id: "tu_1", content: "/tmp" }] } })
  await tick()
  history = await conn.messages(session, 20)
  assert.deepEqual(history.at(-1).parts[0].state, { status: "completed", input: { command: "pwd" }, output: "/tmp" })

  // An aborted request disappears from the dashboard and is denied.
  const aborted = new AbortController()
  const cancelled = q.options.canUseTool("Write", { file_path: "a.txt" }, { signal: aborted.signal })
  aborted.abort()
  assert.equal((await cancelled).behavior, "deny")

  // AskUserQuestion becomes a question card; answers key by question text.
  const questionInput = { questions: [{ question: "Which?", header: "Pick", multiSelect: true,
    options: [{ label: "A", description: "a" }, { label: "B", description: "b" }] }] }
  const answered = q.options.canUseTool("AskUserQuestion", questionInput, { signal: new AbortController().signal })
  const [question] = conn.snapshot().pending.questions
  assert.equal(question.questions[0].multiple, true)
  await conn.execute({ action: "question.reply", sessionID: session.id,
    payload: { requestID: question.id, answers: [["A", "B"]] } }, sessions)
  assert.deepEqual((await answered).updatedInput.answers, { "Which?": "A, B" })

  await conn.execute({ action: "interrupt", sessionID: session.id }, sessions)
  assert.equal(q.interrupted, 1)
  q.emit({ type: "result", subtype: "error_during_execution", is_error: true, session_id: "claude-1", errors: ["interrupted"] })
  await tick()
  assert.equal(conn.snapshot().sessions.find(({ id }) => id === session.id).status, "idle")
  assert.ok(frames.some(({ payload }) => payload.type === "session.error"))

  // A restarted worker resumes the same Claude session and rehydrates history.
  controller.abort()
  await running
  assert.ok(q.ended)
  const restarted = new ClaudeConnection({ ...options, signal: undefined })
  const rehydrated = await restarted.messages(session, 20)
  assert.deepEqual(rehydrated.map(({ parts }) => parts[0].text), ["Hi", "Hello"])
  await restarted.execute({ action: "prompt", sessionID: session.id, payload: { text: "Continue" } }, sessions)
  assert.equal(sdk.queries.at(-1).options.resume, "claude-1")
  for (const live of restarted.live.values()) live.query.end()
})

test("A Claude worker without credentials reports sign-in instead of accepting prompts", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "claude-worker-"))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const sdk = fakeSDK()
  sdk.account = { tokenSource: "none", apiProvider: "firstParty" }
  const conn = new ClaudeConnection({ workspaceRoot: root, stateDirectory: join(root, "state"),
    sessionLimit: 10, messageLimit: 20, claudeSDK: sdk })
  await assert.rejects(conn.stream(() => {}, () => assert.fail("must not become ready")), /not signed in/)
  assert.throws(() => conn.health(), /not signed in/)
})

test("Claude subscription rate limits become worker usage windows and survive restarts", async (t) => {
  const info = { status: "allowed", rateLimitType: "five_hour", resetsAt: 1794772800,
    unifiedWindows: { five_hour: { utilization: 0.59, resetsAt: 1794772800, status: "allowed" },
      seven_day: { utilization: 0.53, resetsAt: 1794880800, status: "allowed" }, overage: { utilization: 2 } } }
  assert.deepEqual(usageFromRateLimit(info, 1794760000000), { sampled_at: 1794760000, windows: [
    { kind: "five_hour", remaining_percent: 41, duration_minutes: 300, resets_at: 1794772800 },
    { kind: "seven_day", remaining_percent: 47, duration_minutes: 10080, resets_at: 1794880800 },
  ] })
  assert.equal(usageFromRateLimit({ status: "allowed" }), undefined)
  assert.equal(usageFromRateLimit({ rateLimitType: "five_hour", utilization: 0.25 }).windows[0].remaining_percent, 75)

  const root = mkdtempSync(join(tmpdir(), "claude-worker-"))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const options = { workspaceRoot: root, stateDirectory: join(root, "state"), sessionLimit: 10,
    messageLimit: 20, claudeSDK: fakeSDK() }
  const conn = new ClaudeConnection(options)
  const session = await conn.execute({ action: "create" }, [])
  conn.observe(session.id, { type: "rate_limit_event", session_id: "claude-9", rate_limit_info: info })
  assert.equal(conn.usage.windows.length, 2)
  assert.deepEqual(new ClaudeConnection(options).usage, conn.usage)
})
