import assert from "node:assert/strict";
import test from "node:test";
import {
  CARD_HANDOFF_CHOICES,
  cardHandoffLabel,
  effectiveHandoffMode,
  HANDOFF_MODES,
  handoffCopy,
  handoffLockReason,
  isCardHandoff,
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

test("a card inherits the project default until it overrides it", () => {
  assert.deepEqual([...CARD_HANDOFF_CHOICES], ["", "off", "semi_auto"]);
  assert.ok(isCardHandoff("") && isCardHandoff("off") && isCardHandoff("semi_auto"));
  assert.equal(isCardHandoff("full_auto"), false);

  // Blank, missing or unrecognised on the card means inherit, matching the
  // server's `card.handoff_mode or plan.handoff_mode`.
  for (const blank of ["", null, undefined, "full_auto"])
    assert.equal(effectiveHandoffMode(blank, "semi_auto"), "semi_auto", String(blank));
  assert.equal(effectiveHandoffMode("", "off"), "off");
  // An override wins over the project default, in both directions.
  assert.equal(effectiveHandoffMode("off", "semi_auto"), "off");
  assert.equal(effectiveHandoffMode("semi_auto", "off"), "semi_auto");
  // An unknown project default cannot resolve to auto-approving.
  assert.equal(effectiveHandoffMode("", "full_auto"), "off");
});

test("the inherit choice names what it inherits", () => {
  assert.equal(cardHandoffLabel("", "off"), "Project default (Off)");
  assert.equal(
    cardHandoffLabel("", "semi_auto"),
    "Project default (Semi-automatic)",
  );
  // An explicit override reads as itself, not as a default.
  assert.equal(cardHandoffLabel("off", "semi_auto"), "Off");
  assert.equal(cardHandoffLabel("semi_auto", "off"), "Semi-automatic");
});
