---
name: task-coordination
description: Use at the start of a work session or when starting, picking up, or continuing a task. Checks the shared task coordinator, claims work safely, and keeps the claim alive.
---

# Task Coordination

The task-coordinator MCP server is the source of truth for live cross-project work.
GitHub Issues holds this repository's specs, documentation, and implementation tickets.
Link the GitHub issue URL in coordinator task descriptions rather than duplicating specs.

## Before starting
1. Call get_overview and read the project's docs_path reference.
2. For a specific task, use list_tasks(project=...) and inspect all pages for a match.
   If it is unclaimed and ready, claim_task. If another agent owns it, stop and tell the user.
   If no matching task exists, create_task with acceptance criteria and the issue URL,
   then claim_task. Do not recreate completed work without explicit instruction.
3. Without a specific task, claim_next_task, optionally scoped to a project.
   A null result means no ready work; report that rather than inventing a claim.
4. Work on only one claimed task unless the user explicitly permits more.

## During work
- heartbeat every 2–5 minutes or between major steps. A long-running command needs
  a heartbeat arrangement; do not assume tool calls automatically renew the claim.
- If your claim expired or heartbeat returns an ownership conflict, stop edits and
  re-check get_task. Another agent may now own the task.
- Avoid areas clearly covered by another agent's in-progress task.
- Create new tasks for discovered extra work instead of silently expanding scope.
- Surface tool failures to the user; do not continue untracked. Never automatically
  retry a failed mutation without checking whether it already succeeded.

## Hand-off mode
- Your claimed task's editable fields change through update_task; status, blockers and
  summaries stay with update_status and release_task.
- handoff_mode is off by default. semi_auto auto-approves binary approve/deny permission
  prompts for sessions linked to that task; set it with create_task or update_task only
  when the user asks for unattended approvals.
- list_pending_prompts shows unresolved prompts for your bound worker and each one's
  promptType. It is read-only: a human answers every prompt, including the
  multiple-choice and free-text ones semi_auto escalates.

## Bootstrap exception
When installing this service itself and MCP is not yet configured, explicitly tell
the user and track the bootstrap in a claimed GitHub issue. This exception ends when
the server is operational. Do not represent GitHub assignment as an atomic MCP claim.
