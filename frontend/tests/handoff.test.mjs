import assert from "node:assert/strict";
import test from "node:test";
import {
  HANDOFF_MODES,
  handoffCopy,
  handoffLockReason,
  isHandoffMode,
  nextHandoffMode,
  normalizeHandoffMode,
} from "../src/handoff.ts";

test("only the two server-side modes are offered", () => {
  assert.deepEqual([...HANDOFF_MODES], ["off", "semi_auto"]);
  assert.ok(isHandoffMode("off") && isHandoffMode("semi_auto"));
  // A mode the server would reject with 400 must never reach the select.
  for (const bad of ["full_auto", "", "OFF", null, undefined, 0, {}])
    assert.equal(isHandoffMode(bad), false, `rejected ${String(bad)}`);
});

test("an unknown server value falls back to the safe mode", () => {
  assert.equal(normalizeHandoffMode("semi_auto"), "semi_auto");
  assert.equal(normalizeHandoffMode("off"), "off");
  // Forward compatibility: a future mode must not render as auto-approving.
  for (const unknown of ["full_auto", undefined, null])
    assert.equal(normalizeHandoffMode(unknown), "off");
});

test("the toggle flips between the two modes", () => {
  assert.equal(nextHandoffMode("off"), "semi_auto");
  assert.equal(nextHandoffMode("semi_auto"), "off");
});

test("copy states what is automated and what still escalates", () => {
  const off = handoffCopy("off");
  assert.equal(off.label, "Off");
  assert.match(off.summary, /waits for a human/);
  // Nothing is automated, so there is no escalation caveat to show.
  assert.equal(off.escalation, "");

  const semi = handoffCopy("semi_auto");
  assert.equal(semi.label, "Semi-automatic");
  assert.match(semi.summary, /approved automatically/);
  // The risky mode must name both halves: what it answers and what it does not.
  assert.match(semi.escalation, /Questions/);
  assert.match(semi.escalation, /deny rule/);
  for (const rule of ["production", "credentials", "destructive"])
    assert.match(semi.escalation, new RegExp(rule));
});

test("viewers and finished tasks are locked, live tasks are editable", () => {
  assert.match(handoffLockReason(false, "in_progress"), /Viewers/);
  assert.match(handoffLockReason(false, "pending"), /members and owners/);
  // A done or cancelled task has no session left for the flag to affect.
  assert.match(handoffLockReason(true, "done"), /cannot change on a done task/);
  assert.match(
    handoffLockReason(true, "cancelled"),
    /cannot change on a cancelled task/,
  );
  for (const status of ["pending", "in_progress", "blocked", "review"])
    assert.equal(handoffLockReason(true, status), null, status);
});
