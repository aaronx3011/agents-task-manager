import assert from "node:assert/strict";
import test from "node:test";
import { mergeLiveMessages } from "../src/session-live.ts";

const saved = { id: 42, remoteID: "msg_a", role: "assistant", publishedAt: "2026-10-25T00:00:00Z",
  content: { time: { created: 1 } }, parts: [{ type: "text", text: "old" }] };
const live = (messages) => ({ worker: "a", sessionID: "s", type: "session.live",
  epoch: "epoch", sequence: 1, messages });

test("live text replaces a saved message by remote identity without losing durable identity", () => {
  const result = mergeLiveMessages([saved], live([
    { ...saved, parts: [{ type: "text", text: "streamed" }] },
    { ...saved, remoteID: "msg_b", content: { time: { created: 2 } } },
  ]));
  assert.equal(result.length, 2);
  assert.equal(result[0].id, 42);
  assert.equal(result[0].publishedAt, saved.publishedAt);
  assert.equal(result[0].parts[0].text, "streamed");
  assert.equal(result[1].remoteID, "msg_b");
});

test("an older partial overlay cannot regress a durably completed message", () => {
  const completed = { ...saved, content: { time: { created: 1, completed: 3 } },
    parts: [{ type: "text", text: "final" }] };
  assert.equal(mergeLiveMessages([completed], live([saved]))[0].parts[0].text, "final");
  assert.deepEqual(mergeLiveMessages([completed], null), [completed]);
});
