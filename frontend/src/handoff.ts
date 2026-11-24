// Hand-off mode presentation (#431, task #438). The policy itself is server-side
// in coordinator/handoff.py; this module only names the modes and says, in the
// user's words, what each one hands to an agent and what it still keeps for a human.
import type { Schemas } from "./api-client";

export type HandoffMode = Schemas["HandoffModeEnum"];

export const HANDOFF_MODES = [
  "off",
  "semi_auto",
] as const satisfies readonly HandoffMode[];

export function isHandoffMode(value: unknown): value is HandoffMode {
  return (HANDOFF_MODES as readonly unknown[]).includes(value);
}

/** The mode a toggle should request next. */
export function nextHandoffMode(mode: HandoffMode): HandoffMode {
  return mode === "semi_auto" ? "off" : "semi_auto";
}

export interface HandoffCopy {
  /** Short name for the badge and the switch. */
  label: string;
  /** What this mode does, in one sentence. */
  summary: string;
  /** What still reaches a human regardless. Empty when nothing is automated. */
  escalation: string;
}

const COPY: Record<HandoffMode, HandoffCopy> = {
  off: {
    label: "Off",
    summary: "Every permission request and question waits for a human.",
    escalation: "",
  },
  semi_auto: {
    label: "Semi-automatic",
    summary:
      "Approve/deny permission requests from this task's sessions are approved automatically.",
    escalation:
      "Questions, multiple-choice and free-text prompts, and anything matching a deny rule (production, deploy, DNS, billing, credentials, destructive git or delete) still wait for a human.",
  },
};

export function handoffCopy(mode: HandoffMode): HandoffCopy {
  return COPY[mode];
}

/** Treat an unknown server value as the safe mode rather than rendering nothing. */
export function normalizeHandoffMode(value: unknown): HandoffMode {
  return isHandoffMode(value) ? value : "off";
}

/** A planning card's own choice: a mode, or "" to inherit the project default (#137). */
export type CardHandoff = HandoffMode | "";

export const CARD_HANDOFF_CHOICES = [
  "",
  ...HANDOFF_MODES,
] as const satisfies readonly CardHandoff[];

export function isCardHandoff(value: unknown): value is CardHandoff {
  return value === "" || isHandoffMode(value);
}

/** What a card will actually launch with. Mirrors services._card_handoff. */
export function effectiveHandoffMode(
  cardMode: unknown,
  projectMode: unknown,
): HandoffMode {
  // Blank, missing or unrecognised on the card means inherit, exactly as the
  // server's `card.handoff_mode or plan.handoff_mode` does.
  const card = isCardHandoff(cardMode) ? cardMode : "";
  return card === "" ? normalizeHandoffMode(projectMode) : card;
}

/** Label for a card's override selector, where blank names what it inherits. */
export function cardHandoffLabel(
  value: CardHandoff,
  projectMode: HandoffMode,
): string {
  return value === ""
    ? `Project default (${handoffCopy(projectMode).label})`
    : handoffCopy(value).label;
}

/** Why the control is unavailable, or null when the user may change it. */
export function handoffLockReason(
  canWrite: boolean,
  status: Schemas["Task"]["status"],
): string | null {
  if (!canWrite)
    return "Viewers can see hand-off mode; members and owners change it.";
  // A finished task has no sessions left to answer prompts for, so offering the
  // switch would imply an effect it cannot have.
  if (status === "done" || status === "cancelled")
    return `Hand-off mode cannot change on a ${status} task.`;
  return null;
}
