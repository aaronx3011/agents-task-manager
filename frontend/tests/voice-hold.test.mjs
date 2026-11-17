import assert from "node:assert/strict";
import test from "node:test";
import { HoldToTalk, pointerWithinRect } from "../src/voice-hold.ts";

test("pointer capture ownership sends once; outside release cancels", () => {
  const hold = new HoldToTalk();
  assert.equal(hold.beginPointer(1), true);
  assert.equal(hold.beginPointer(2), false);
  assert.equal(hold.endPointer(2, true), "none");
  assert.equal(hold.endPointer(1, true), "send");
  assert.equal(hold.endPointer(1, true), "none");
  hold.beginPointer(3);
  assert.equal(hold.endPointer(3, false), "cancel");
  assert.equal(hold.active, false);
});

test("keyboard repeats and mixed inputs cannot duplicate a turn; cancellation consumes release", () => {
  const hold = new HoldToTalk();
  assert.equal(hold.beginKeyboard(), true);
  assert.equal(hold.beginKeyboard(), false);
  assert.equal(hold.beginPointer(1), false);
  assert.equal(hold.endKeyboard(), "send");
  assert.equal(hold.endKeyboard(), "none");
  hold.beginKeyboard();
  assert.equal(hold.cancel(), "cancel");
  assert.equal(hold.endKeyboard(), "none");
  assert.equal(hold.cancel(), "none");
});

test("release coordinates, including captured releases outside, determine cancellation", () => {
  const rect = { left: 10, right: 100, top: 20, bottom: 80 };
  assert.equal(pointerWithinRect(rect, 10, 80), true);
  assert.equal(pointerWithinRect(rect, 50, 50), true);
  for (const [x, y] of [[9, 50], [101, 50], [50, 19], [50, 81]])
    assert.equal(pointerWithinRect(rect, x, y), false);
});
