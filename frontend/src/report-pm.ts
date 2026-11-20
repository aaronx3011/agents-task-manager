// The PM router's chat endpoint accepts at most 4000 characters per turn.
export const PM_TEXT_LIMIT = 4000;

export type PmAction = "create" | "queue";
export type PmReport = {
  action: PmAction;
  project: string;
  title: string;
  description: string;
  priority: number;
  worker: string;
  model: string;
};
export type PmReply = {
  text: string;
  events: { tool: string; is_error: boolean }[];
};
export type PmOutcome = {
  ok: boolean;
  // queued: accepted onto the durable queue, execution NOT complete.
  kind: "created" | "queued" | "refused" | "no-action";
  message: string;
};

const EXPECTED_TOOL: Record<PmAction, string> = {
  create: "create_task",
  queue: "launch_task",
};

/** Deterministic instruction for the existing PM router; fields are JSON-quoted. */
export function buildPmPrompt(report: PmReport) {
  const fields: Record<string, string | number> = {
    project: report.project,
    title: report.title.trim(),
    description: report.description.trim(),
    priority: report.priority,
  };
  if (report.action === "queue") {
    if (report.worker.trim()) fields.worker = report.worker.trim();
    if (report.model.trim()) fields.model = report.model.trim();
  }
  const instruction =
    report.action === "queue"
      ? "Dashboard report. Call launch_task once to create this task and queue it for execution (play the queue). Use exactly these JSON-quoted fields, do not alter them, and if the worker is offline, the model is unknown or the queue is empty or refused, say so plainly and do not retry elsewhere."
      : "Dashboard report. Call create_task once to create this backlog task only. Do not launch or queue it. Use exactly these JSON-quoted fields and do not alter them.";
  return `${instruction}\n${JSON.stringify(fields)}`;
}

export function pmPromptTooLong(report: PmReport) {
  return buildPmPrompt(report).length > PM_TEXT_LIMIT;
}

/** Judge the real tool events, never the model's prose, for what happened. */
export function classifyPmReply(action: PmAction, reply: PmReply): PmOutcome {
  const text = reply.text.trim();
  const failed = reply.events.filter((event) => event.is_error);
  if (failed.length)
    return {
      ok: false,
      kind: "refused",
      message:
        `The PM router refused or failed (${failed.map((e) => e.tool).join(", ")}); nothing was queued. ${text}`.trim(),
    };
  const done = reply.events.some(
    (event) => event.tool === EXPECTED_TOOL[action],
  );
  if (!done)
    return {
      ok: false,
      kind: "no-action",
      message:
        `The PM router did not perform the requested action, so no task was created or queued. ${text}`.trim(),
    };
  return action === "queue"
    ? {
        ok: true,
        kind: "queued",
        message:
          `Accepted and queued by the PM router; execution has not completed. ${text}`.trim(),
      }
    : {
        ok: true,
        kind: "created",
        message:
          `Backlog task created by the PM router; not launched. ${text}`.trim(),
      };
}
