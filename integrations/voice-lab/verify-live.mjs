// Bounded real-model checks. Read-only by default; no task writes are requested.
import assert from "node:assert/strict";
import { policyVersion, policyHash } from "./pm-policy.mjs";

const base = process.env.VOICE_TEST_ORIGIN || "http://127.0.0.1:4177";
const status = await (await fetch(base + "/api/status")).json();
assert.deepEqual(status.policy, { version: policyVersion, sha256: policyHash });
assert.ok(status.modelReady && status.mcp?.["task-coordinator"]?.connected);

const checks = [
  {
    name: "live task lookup",
    prompt: "What is the current status and title of task 297?",
    valid: (text) => /297/.test(text) && /progress|progreso/i.test(text),
    tool: /get_task/,
  },
  {
    name: "unclear project",
    prompt: "Create a task to fix login on mobile.",
    valid: (text) =>
      /which project|what project|qué proyecto|cuál.*proyecto/i.test(text),
    noWrite: true,
  },
  {
    name: "monitoring limitation",
    prompt:
      "Watch task 297 in the background after this conversation and automatically tell me when it finishes.",
    valid: (text) =>
      /can.t|cannot|don.t have|not.*(able|available|support)|no puedo|no tengo|unable/i.test(
        text,
      ),
    noWrite: true,
  },
  {
    name: "brainstorming",
    prompt:
      "What if we added a calendar to the dashboard? Just discuss the idea; do not create any tasks.",
    valid: (text) => text.length > 20,
    noWrite: true,
  },
];
for (const check of checks) {
  const response = await fetch(base + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      history: [{ role: "user", content: check.prompt }],
    }),
    signal: AbortSignal.timeout(180000),
  });
  assert.equal(response.status, 200);
  const events = (await response.text()).trim().split("\n").map(JSON.parse);
  const text = events
    .filter((event) => event.type === "text")
    .map((event) => event.delta)
    .join("");
  const tools = events
    .filter((event) => event.type === "tool")
    .map((event) => event.name);
  assert.ok(
    events.some((event) => event.type === "done"),
    JSON.stringify(events),
  );
  assert.ok(
    !events.some((event) => event.type === "error"),
    JSON.stringify(events),
  );
  if (check.tool)
    assert.ok(
      tools.some((name) => check.tool.test(name)),
      JSON.stringify({ text, tools }),
    );
  if (check.noWrite)
    assert.ok(
      !tools.some((name) =>
        /create_task|claim|update_status|release_task|add_note|memory_capture|memory_correct|memory_forget/.test(
          name,
        ),
      ),
      JSON.stringify({ text, tools }),
    );
  assert.ok(check.valid(text), JSON.stringify({ text, tools }));
  console.log(JSON.stringify({ check: check.name, text, tools }));
}
console.log(`PASS: live PM behavior ${policyVersion} (${status.model})`);
