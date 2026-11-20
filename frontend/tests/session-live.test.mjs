import assert from "node:assert/strict";
import test from "node:test";
import { mergeLiveMessages } from "../src/session-live.ts";
import {
  mergeSessionPins,
  rememberSession,
} from "../src/session-library-state.ts";

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

test("recent sessions keep worker identity, dedupe revisits and never own pin state", () => {
  const first = { worker: "a", sessionID: "shared", title: "First" };
  const other = { worker: "b", sessionID: "shared", title: "Other worker" };
  let entries = rememberSession([], first, 1);
  entries = rememberSession(entries, other, 2);
  entries = rememberSession(entries, { ...first, title: "Renamed" }, 3);
  assert.equal(entries.length, 2);
  assert.equal(entries[0].title, "Renamed");
  assert.equal(entries[0].worker, "a");
  assert.equal(entries[1].worker, "b");
  assert.ok(entries.every((entry) => entry.pinned === false));
});

test("three parallel windows stay bounded after 30,000 distinct message updates", () => {
  const windows = [[], [], []];
  for (let sequence = 1; sequence <= 10000; sequence++) {
    for (let panel = 0; panel < 3; panel++) {
      const message = {
        ...saved,
        remoteID: `${panel}:${sequence}`,
        content: { time: { created: sequence } },
      };
      windows[panel] = mergeLiveMessages(windows[panel], live([message]), 50);
      assert.ok(windows[panel].length <= 50);
    }
  }
  windows.forEach((window, panel) => {
    assert.equal(window.length, 50);
    assert.equal(window[0].remoteID, `${panel}:9951`);
    assert.equal(window.at(-1).remoteID, `${panel}:10000`);
    assert.ok(
      window.every((message) => message.remoteID.startsWith(`${panel}:`)),
    );
  });
});

test("the API is the only pin source of truth and keeps its order", () => {
  const first = { worker: "a", sessionID: "first", title: "First" };
  // A stale browser cache still claims a pin; it must not win over the API.
  const stale = [{ ...first, pinned: true, openedAt: 1 }];
  const pins = [
    {
      worker: "b",
      sessionID: "first",
      title: "Server first",
      pinned_at: "2026-11-20T04:00:00Z",
    },
    {
      worker: "a",
      sessionID: "second",
      title: "Server second",
      pinned_at: "2026-11-20T03:00:00Z",
    },
  ];
  const entries = mergeSessionPins(rememberSession(stale, first, 2), pins);
  assert.deepEqual(
    entries.filter((entry) => entry.pinned).map((entry) => entry.title),
    ["Server first", "Server second"],
  );
  assert.equal(
    entries.filter((entry) => entry.pinned).map((entry) => entry.worker)[0],
    "b",
  );
  // The stale local claim survives only as an unpinned recent entry.
  assert.deepEqual(
    entries
      .filter((entry) => entry.sessionID === "first")
      .map((entry) => [entry.worker, entry.pinned]),
    [
      ["b", true],
      ["a", false],
    ],
  );
  assert.ok(entries.slice(2).every((entry) => entry.pinned === false));
});

test("background metadata refreshes keep server pin positions stable", () => {
  const pins = [
    {
      worker: "a",
      sessionID: "first",
      title: "First",
      pinned_at: "2026-11-20T04:00:00Z",
    },
    {
      worker: "a",
      sessionID: "second",
      title: "Second",
      pinned_at: "2026-11-20T03:00:00Z",
    },
  ];
  const before = mergeSessionPins([], pins).map((entry) => entry.sessionID);
  const after = mergeSessionPins(
    rememberSession(
      [],
      { worker: "a", sessionID: "first", title: "Renamed" },
      3,
    ),
    pins,
  );
  assert.deepEqual(
    after.map((entry) => entry.sessionID),
    before,
  );
  assert.equal(after[0].title, "First");
});
