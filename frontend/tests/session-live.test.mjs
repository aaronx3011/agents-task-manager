import assert from "node:assert/strict";
import test from "node:test";
import { mergeLiveMessages } from "../src/session-live.ts";
import { rememberSession } from "../src/session-library.ts";

const saved = {
  id: 42,
  remoteID: "msg_a",
  role: "assistant",
  publishedAt: "2026-10-25T00:00:00Z",
  content: { time: { created: 1 } },
  parts: [{ type: "text", text: "old" }],
};
const live = (messages) => ({
  worker: "a",
  sessionID: "s",
  type: "session.live",
  epoch: "epoch",
  sequence: 1,
  messages,
});

test("live text replaces a saved message by remote identity without losing durable identity", () => {
  const result = mergeLiveMessages(
    [saved],
    live([
      { ...saved, parts: [{ type: "text", text: "streamed" }] },
      { ...saved, remoteID: "msg_b", content: { time: { created: 2 } } },
    ]),
  );
  assert.equal(result.length, 2);
  assert.equal(result[0].id, 42);
  assert.equal(result[0].publishedAt, saved.publishedAt);
  assert.equal(result[0].parts[0].text, "streamed");
  assert.equal(result[1].remoteID, "msg_b");
});

test("an older partial overlay cannot regress a durably completed message", () => {
  const completed = {
    ...saved,
    content: { time: { created: 1, completed: 3 } },
    parts: [{ type: "text", text: "final" }],
  };
  assert.equal(
    mergeLiveMessages([completed], live([saved]))[0].parts[0].text,
    "final",
  );
  assert.deepEqual(mergeLiveMessages([completed], null), [completed]);
});

test("session pins preserve worker identity and revisits do not duplicate saved entries", () => {
  const first = { worker: "a", sessionID: "shared", title: "First" };
  const other = { worker: "b", sessionID: "shared", title: "Other worker" };
  let entries = rememberSession([], first, 1);
  entries = rememberSession(entries, first, 2, true);
  entries = rememberSession(entries, other, 3);
  entries = rememberSession(entries, { ...first, title: "Renamed" }, 4);
  assert.equal(entries.length, 2);
  assert.equal(entries[0].title, "Renamed");
  assert.equal(entries[0].pinned, true);
  assert.equal(entries[1].worker, "b");
  entries = rememberSession(entries, first, 5, true);
  assert.equal(entries.find((entry) => entry.worker === "a").pinned, false);
});
