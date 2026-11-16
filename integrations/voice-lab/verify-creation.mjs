// Explicit write smoke: creates one uniquely labeled verification task and
// asks the same agent to claim/cancel only that task. Never retries creation.
import assert from "node:assert/strict";
const base = process.env.VOICE_TEST_ORIGIN || "http://127.0.0.1:4177";
if (!process.argv.includes("--allow-write"))
  throw new Error(
    "Pass --allow-write to create and cancel a real verification task",
  );
const history = [];
async function turn(content) {
  history.push({ role: "user", content });
  const response = await fetch(base + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ history }),
    signal: AbortSignal.timeout(180000),
  });
  const events = (await response.text()).trim().split("\n").map(JSON.parse);
  const text = events
    .filter((event) => event.type === "text")
    .map((event) => event.delta)
    .join("");
  history.push({ role: "assistant", content: text });
  console.log(
    JSON.stringify({
      text,
      tools: events
        .filter((event) => event.type === "tool")
        .map((event) => event.name),
    }),
  );
  return events;
}
function objects(result) {
  const values = [result?.structuredContent];
  for (const content of result?.content || []) {
    if (content.type === "text") {
      try {
        values.push(JSON.parse(content.text));
      } catch {
        /* not JSON */
      }
    }
  }
  return values.filter(Boolean);
}
const title = `[verification] PM router creation ${new Date().toISOString()}`;
const created = await turn(
  `Create exactly one task in the registered agents-task-manager project titled "${title}". Description: "Temporary PM router release smoke test; cancel after verification, no implementation required." Priority 9999. Do not launch execution or create any other tasks.`,
);
const results = created.filter(
  (event) => event.type === "tool" && /__create_task$/.test(event.name),
);
assert.equal(
  results.length,
  1,
  "Expected exactly one real creation; inspect current tasks before attempting again",
);
const task = objects(results[0].result).find(
  (value) => value.id && value.title === title,
);
assert.ok(
  task?.id,
  "Could not identify created task; inspect current state rather than retrying creation",
);
assert.equal(task.project, "agents-task-manager");
console.log(`Created verification task ${task.id}`);
await turn(
  `Verification is complete. Claim only verification task ${task.id}, titled "${title}", then set that task to cancelled with the reason "PM router creation smoke completed; no implementation needed". Do not claim or modify any other task.`,
);
const final = await turn(
  `Read task ${task.id} using get_task and confirm its status. Do not make any changes.`,
);
const taskRead = final
  .filter((event) => event.type === "tool" && /__get_task$/.test(event.name))
  .flatMap((event) => objects(event.result))
  .find((value) => value.id === task.id);
assert.equal(
  taskRead?.status,
  "cancelled",
  `Verification task ${task.id} needs cancellation`,
);
console.log(`PASS: real creation and cancellation of task ${task.id}`);
