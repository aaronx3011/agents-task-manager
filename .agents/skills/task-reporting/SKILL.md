---
name: task-reporting
description: Use when progress happens on a claimed task, when blocked, handing off, pausing, or finishing. Reports status to the coordinator and updates the project's stable documentation.
---

# Task Reporting

## Blocked or waiting
1. When another project needs work, create_task there with precise acceptance criteria.
2. MCP create_task accepts depends_on at creation time. For an already-existing task,
   add_note with the blocker ID and ask a human to add the dependency in admin,
   or use an explicitly available REST client to PATCH depends_on as the owner.
   PATCH replaces the whole prerequisite list; preserve existing prerequisites.
   Do not imply that update_status modifies dependencies.
3. update_status(blocked, reason=...) with the blocker ID/issue URL and required outcome.
4. Tell the user what is needed. Blocked claims remain assigned until explicitly released.

## Progress
Use add_note for decisions, discoveries, artifacts and partial results useful to a successor.
Keep GitHub issue progress in sync, referencing the coordinator task ID.

## Pausing or handing off
Use release_task(reason=...) with what was done and what remains. This returns the
task to pending. If dependencies are not yet recorded, explain that to the user
before releasing so another agent does not unknowingly pick up blocked work.

## Finishing
1. update_status(done, result_summary=...) or review when a human should check the result.
2. Include changed behavior, files/PRs/assets, verification and follow-ups.
3. Update the project's docs_path reference with stable implemented behavior.
   This repository uses GitHub documentation issues, not local Markdown work tickets.
4. Create follow-up coordinator tasks and GitHub issues for remaining work.

Never leave a task in_progress when stopping. Review retains ownership for the
review process; the same agent may finish it or a human may override in admin.
Tool failures must be surfaced; do not silently assume status reporting succeeded.
