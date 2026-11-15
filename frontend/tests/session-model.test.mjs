import assert from "node:assert/strict";
import test from "node:test";
import {
  composerModel,
  conversationModel,
  modelPayload,
} from "../src/session-model.ts";
import { mergeLiveMessages } from "../src/session-live.ts";

const workerDefault = { providerID: "worker", modelID: "default" };
const active = { providerID: "openai", modelID: "coding/model" };
const other = { providerID: "other", modelID: "next" };
const message = (role, content, created = 1) => ({
  id: created,
  remoteID: `msg_${created}`,
  role,
  content: { time: { created }, ...content },
  parts: [],
  publishedAt: "2026-11-10T00:00:00Z",
});

test("reload restores the conversation model and sends it instead of the worker default", () => {
  const messages = [message("user", { model: active })];
  for (let mount = 0; mount < 2; mount++) {
    const observed = conversationModel(messages, null);
    const value = composerModel(undefined, observed);
    assert.equal(value, "openai/coding/model");
    assert.deepEqual(modelPayload(value, [workerDefault, active], observed), {
      model: active,
    });
  }
});

test("live model changes supersede stale session metadata and saved history", () => {
  const saved = [message("assistant", active)];
  const live = {
    messages: [message("user", { model: other }, 2)],
  };
  const observed = conversationModel(mergeLiveMessages(saved, live), active);
  assert.deepEqual(observed, other);
  assert.equal(composerModel(undefined, observed), "other/next");
  // Polling/live updates must not erase an explicit next-message selection.
  assert.equal(composerModel("openai/coding/model", observed), "openai/coding/model");
  assert.equal(composerModel("", observed), "");
  assert.deepEqual(modelPayload("", [workerDefault, active], observed), {});
});

test("assistant metadata, partial windows and models absent from the catalog are retained", () => {
  const observed = conversationModel(
    [
      message("assistant", active),
      message("assistant", { ...other, summary: true }, 2),
      message("user", { model: { providerID: "incomplete" } }, 3),
    ],
    workerDefault,
  );
  assert.deepEqual(observed, active);
  assert.deepEqual(modelPayload(composerModel(undefined, observed), [], observed), {
    model: active,
  });
  assert.deepEqual(conversationModel([], other), other);
  assert.equal(conversationModel([], null), null);
  assert.equal(composerModel(undefined, null), "");
});

test("switching sessions with a fresh composer follows each session independently", () => {
  assert.equal(composerModel(undefined, active), "openai/coding/model");
  assert.equal(composerModel(undefined, other), "other/next");
  assert.equal(composerModel(undefined, active), "openai/coding/model");
});
