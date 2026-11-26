# Agent Task Manager

Self-hosted coordination for agents and a human workspace for tasks, workers,
OpenCode sessions, and pending interactions.

This is a local onboarding and source-navigation guide, reviewed on 2026-10-25.
The authoritative architecture/API contract is [GitHub issue #1](https://github.com/aaronx3011/agents-task-manager/issues/1);
setup and operations are maintained in [issue #3](https://github.com/aaronx3011/agents-task-manager/issues/3).
Documentation work is tracked in [issue #22](https://github.com/aaronx3011/agents-task-manager/issues/22).

## Projects and ownership

| Component | Owns | Entry point |
| --- | --- | --- |
| Django coordinator | Task claims, dependencies, audit, worker registry, commands, bounded session read models | `coordinator/` |
| React workspace | Human task/session views and interaction controls | `frontend/` |
| MCP adapter | Agent-facing tools over authenticated coordinator HTTP | `mcp_server.py` |
| Worker bridge | Outbound sync, local OpenCode calls, durable command receipts | `bridge/worker.mjs` |
| OpenCode | Model execution, tools, filesystem, permission decisions, canonical session history | [Client project guide](../open/opencode_repo/COORDINATOR_FLOW.md) |

Django does not execute model calls or filesystem tools. Worker connectivity,
OpenCode reachability, session execution state, and task ownership are separate.

## End-to-end flow

```text
Agent -> stdio MCP adapter -> Django REST -> task services/database
Human browser -> Django staff session -> workspace/read APIs/command queue
Human browser <- WebSocket invalidation <- committed coordinator changes

Worker bridge -> POST /api/worker/sync -> Django
Worker bridge <- acknowledged batch + queued commands <- Django
Worker bridge -> local HTTP requests -> OpenCode execution
Worker bridge <- HTTP/SSE, session snapshots, pending requests <- OpenCode

Worker bridge -> /ws/worker/ -> shared live cache -> focused browser subscription
Worker bridge -> periodic HTTP message batch -> database
```

The bridge initiates the connection to Django. Browsers use Django rather than
connecting directly to worker machines. Provider and OpenCode credentials stay
local; the bridge authenticates to Django with its bound agent's key.

### Task lifecycle

1. Read the overview and ready tasks; create a scoped task linked to its GitHub
   issue when the requested work has no existing task.
2. Claim a task atomically. Ready means `pending`, unclaimed, with every
   prerequisite `done`. Smaller priority values come first.
3. Work while sending heartbeats every 2–5 minutes and recording discoveries.
   Claims expire after 15 minutes without a heartbeat when reaping runs.
4. Use `blocked` with a reason, `review` or `done` with a result summary, or
   release the task back to pending with handoff details.

The claiming agent controls owned task mutations; staff may override. Task
heartbeats are independent of worker sync. Session idleness and accepted prompts
do not mark tasks done. Execution attempts link tasks to create/prompt commands
and observed sessions without changing those ownership rules.

### Questions and permission approvals

1. OpenCode produces a pending question batch or a permission request.
2. The bridge publishes pending requests with worker/session/request identity.
   A complete snapshot can resolve missing requests; incomplete snapshots cannot.
3. Django exposes pending interactions to the human workspace.
4. A response creates a command through `POST /api/commands`:
   `question.reply`, `question.reject`, or `permission.reply`.
5. The bridge receives the command, re-fetches local pending requests, checks
   session ownership, and submits the response to OpenCode.
6. Results and subsequent session observations return through worker sync.

Question replies contain ordered answer arrays, honoring each question's
single/multiple/custom rules. Permission replies use `once`, `always`, or
`reject`. A question labeled “Approve” is still a question; filesystem approval
requires an actual permission request from OpenCode's permission policy. A file
copy may execute without prompting when the effective policy already permits it.

### Semi-automatic hand-off mode

Each task carries `handoff_mode`, either `off` (the default) or `semi_auto`. With
`semi_auto`, a binary approve/deny **permission** request from a session linked to
that task is auto-answered `once`, audited on the task as `handoff.auto_answer`
and streamed as `handoff.auto_answered`. Everything else still waits for a human
and is published as `handoff.escalated`: questions of any shape (including one
titled “Approve”), sessions not linked to a `semi_auto` task, and prompt records
matching a deny rule — `production`, `deploy`, `dns`, `billing`, `credentials`,
`destructive_git`, `destructive_delete`, searched case-insensitively across the
whole record and replaceable with the `HANDOFF_DENY_RULES` setting. The flag
changes who answers an approve/deny prompt, never what the permission policy asks.
Requires migration `0023`. Source: `coordinator/handoff.py`; tests:
`tests/test_handoff.py`.

Pending interactions expose `promptType` (`approve_deny`, `multiple_choice`,
`free_text`, `unknown`) so clients can tell an auto-answerable prompt from one that
must be escalated.

**Setting it before launch.** A project's planning settings carry a **Prompt
approvals** default (`ProjectPlan.handoff_mode`), and each card may override it
(`PlanningCard.handoff_mode`, blank inherits), exactly as worker/agent/model
already work. The resolved mode is captured into the job snapshot at enqueue,
refreshes while the job is still queued, and is applied to the task the runner
creates — or to a reused task, audited as a task note, since the card's
configuration governs what a launch does. Unlike agent and model this is not a
worker capability, so a card on a non-default worker still inherits the project's
choice. Cards whose effective mode is `semi_auto` are badged on the board, so an
unattended card is identifiable without opening it. Requires migration `0024`.

**Setting it on a running task.** Open a task and use **Hand-off mode → Prompt approvals** in the
task detail modal; the control states what the selected mode automates and what
still waits for a human, and the current mode shows as a badge beside the heading.
Members and owners can change it, viewers see it read-only, and a `done` or
`cancelled` task is locked because it has no session left for the flag to affect.
The modal writes `PATCH /api/v1/tasks/{id}`, so the API's existing rules apply
unchanged and a refused edit surfaces the server's message instead of failing
quietly. It is also reachable from `create_task`/`update_task` over MCP (below),
the REST API directly, and Django admin. Source: `frontend/src/handoff.ts` and
`frontend/src/TaskModal.tsx`; checks: `frontend/tests/handoff.test.mjs` and
`scripts/verify_handoff_mode_browser.py`. The mobile app does not surface the flag.

### MCP tools

`mcp_server.py` is a thin, HTTP-only adapter over the authenticated coordinator
API. Alongside the lifecycle tools (`get_overview`, `list_tasks`, `get_task`,
`create_task`, `claim_next_task`, `claim_task`, `heartbeat`, `update_status`,
`release_task`, `add_note`, `report_planning_job`) it exposes hand-off mode,
read-only prompt lookup and read-only pipe inspection:

- `create_task` takes an optional `handoff_mode` (`off` default, or `semi_auto`).
- `update_task` PATCHes a claimed task's editable fields — `title`,
  `description`, `project`, `priority`, `depends_on`, `handoff_mode` — leaving
  omitted arguments unchanged. Only the claiming agent may edit a task; another
  agent's task is rejected with HTTP 403 and an unknown mode with HTTP 400,
  including the rejecting field name. Status, blockers and summaries stay with the
  lifecycle tools.
- `list_pending_prompts` reads unresolved prompts for the caller's bound worker,
  each with its `promptType`.
- `list_pipes(project, limit=20, offset=0)` reads a project's pipes newest first,
  each with ordered stages, worker, agent, model, directory, status, artifact,
  latest job/session and task status.
- `get_pipe(project, pipe_id)` reads one pipe; a pipe belonging to another
  project is refused with HTTP 404.
- `get_task` and `list_tasks` return `handoff_mode` on every task.

Pipe tools are **read-only by authorization decision**. Agent Bearer keys are not
staff sessions and carry no owner/CSRF queue-control capability, so exposing
`create_pipe`, `approve_pipe` or `stop_pipe` would silently widen agent access to
worker execution. Those mutations remain on the staff-session planning API
(`pipe_create`, `pipe_approve`, `pipe_stop`) and the owner-only PM voice toolset.
The read endpoints are `GET /api/projects/{slug}/pipes` and
`GET /api/projects/{slug}/pipes/{id}`; they accept agent keys or signed-in
viewers, refuse anonymous callers and all writes, and are project-scoped.

Chat rooms are read-only in the MCP for the same reason, plus two of their own:
posting into a room spends model budget, and an acting participant mutates with
its owner's authority. `list_chat_rooms`, `get_chat_room` and
`read_chat_messages` call `GET /api/v1/chat/rooms`, `.../chat/rooms/{id}` and
`.../chat/rooms/{id}/messages`.

Answering prompts is deliberately **not** an MCP tool: `permission.reply`,
`question.reply` and `question.reject` stay with humans through
`POST /api/v1/commands` and the web and mobile inboxes, so the multiple-choice and
free-text prompts hand-off mode escalates are not answerable by an agent.

**MCP tools ship with the local checkout, not the containers.** A development or
production deploy cannot deliver a new MCP tool. Each agent must update its
checkout (or installed adapter) and reload/restart its MCP connection after
upgrading to discover `list_pipes`, `get_pipe`, `list_chat_rooms`,
`get_chat_room` and `read_chat_messages`.

### Delivery and recovery

- Commands follow `queued -> dispatched -> done | error`. For an asynchronous
  prompt, `done` means accepted by OpenCode, not a completed assistant answer.
- The bridge persists intent before execution and outcome before reporting.
  Interrupted intents become uncertain; commands must not be blindly replayed.
- Sync retries retain the same serialized envelope/batch ID until a matching
  acknowledgement. Epoch changes trigger snapshot republishing.
- Session/message/event windows are bounded. OpenCode remains the history owner;
  cursor gaps and incomplete windows must remain visible to consumers.
- The packaged adapter targets installed OpenCode **1.18.32**. The sibling
  OpenCode checkout declares **2.1.1**; its newer client/API is a separate
  compatibility surface, not a drop-in bridge target.

### Focused-session streaming and batched storage

The dashboard polls session summaries every 5 seconds and uses its existing
staff WebSocket for one selected `(worker, session)` subscription per tab.
Live message windows overlay saved history by remote message identity; switching
sessions replaces the subscription. Reconnecting refetches history and subscribes
again, while hidden tabs unsubscribe. Questions, permissions, command changes,
and execution failures continue to invalidate workspace views immediately.

The bridge negotiates a separate outbound `/ws/worker/` connection. It coalesces
message events into live snapshots at 100 ms sampling intervals without writing
message rows. Redis holds a 15-second live overlay shared across ASGI workers;
single-process development uses bounded local memory. Snapshot sequence/epoch
identity suppresses duplicate same-stream frames; this is latest-state delivery,
not a durable event replay protocol. Cache expiry or socket gaps recover from
the database and subsequent OpenCode reconciliation.

HTTP sync persists changed windows every 5 seconds by default, with early
flushes for terminal/interaction events, byte thresholds, and command results.
Each batch bulk-inserts/updates the latest message states; unchanged content is
not rewritten. Commands remain durably admitted through HTTP and dispatched by
the existing acknowledged sync; the socket wakes that sync immediately.

### Chat history and saved sessions

- Conversation panels load the latest 10 stored messages first
  (`GET .../messages?latest=true`) and fetch strictly older pages with the
  exclusive `before` cursor (`timeline_time:pk`) as the reader scrolls up, keeping
  the viewport anchored while pages prepend. The panel follows the newest message
  from the bottom and offers a jump back when reading history.
- Compact session details (`GET .../sessions/<id>?compact=true`) keep only the
  reply commands referenced by pending interactions; unrelated chat command
  history stays out of large reads.
- The unified session list supports `GET /api/sessions?ordering=recent` (remote
  update order, newest first). The Sessions section combines it with a
  per-staff-user browser library (`frontend/src/session-library.ts`) that pins up
  to 100 and remembers 50 recently focused sessions locally; pinning is
  navigation/bookkeeping only and never bulk-loads full histories.
- Worker commands and their results moved to the Activity section
  (`frontend/src/ActivityPage.tsx`, `GET /api/commands`), keeping chat panels
  focused on conversations.

## Project planning and execution queues

Open **Planning** in the sidebar/mobile navigation, or
`/?section=planning&project=PROJECT_SLUG`.

Create projects directly with **New project** in Planning or the **+** beside
Projects in the sidebar. Enter a name, suggested/editable unique slug, kind and
optional repository/documentation references. Creation selects the project,
refreshes navigation and opens its execution settings. The form is available
even when the workspace has no projects. Duplicate slugs are rejected without
changing the existing project. This uses staff-session-only `POST /api/projects`
with CSRF; it provisions no agents, credentials or worker processes.

1. In **Settings**, choose the project's default worker, workspace directory,
   agent and model. Add project context, standing instructions, reusable prompt
   templates and board columns. Agent and model choices come from the selected
   worker's connected OpenCode catalogs; leaving them blank uses the worker
   defaults.
2. Create cards with descriptions/acceptance criteria, labels and checklists.
   Each card can override the project agent and model for that task only; the
   override never rewrites the project defaults and is captured with the job.
   Drag cards between columns or use their move menu. Planning columns are
   independent of task lifecycle status; cards enter the coordinator task pool
   only when execution starts, where creation and claim happen atomically.
3. Choose **Run / Queue**, select an optional prompt template and add your
   instructions. **Start session** runs one card when the queue is empty;
   **Add to queue** captures its settings/content for later execution.
4. In **Queue**, reorder waiting tasks and press **Play**. Each task gets a new
   session. Only explicit task `done` advances to the next task; idle sessions and
   accepted prompts never count as completion. Previous completed results are
   added to the next prompt as bounded handoff context.
5. **Pause** allows current work to finish but prevents the next launch.
   **Stop current** queues an interrupt and pauses; resolve the stopped task and
   any uncertain commands before continuing. Questions/permissions, blocked or
   review tasks, execution errors and overdue command acknowledgements pause the
   queue. Answer/resolve the issue, then press Play explicitly.

The queue is durable and advances during authenticated worker sync, including
after browser closure or coordinator restarts. It needs an enabled connected
worker bridge. Existing queued jobs retain their captured worker, directory,
agent, model and instructions when project settings change; remove/re-add a
waiting job to capture new settings. The first session prompt starts with the
task agent and model, which can still be changed from the session afterwards.
The full assembled prompt is inspectable in the queue.
First prompts have no size limit: project settings, the card and up to ten previous
handoffs (2,000 characters each) are sent whole.

The additional MCP tool **`report_planning_job`** accepts a job-specific token
provided in the first prompt. An authenticated agent can report heartbeat,
done/blocked/review for that active job even when its MCP identity differs from
the worker's bound agent. This delegates reporting only; it does not grant task
claims or access to other jobs. Task ownership stays with the bound agent and
worker sync never renews claims. Reload the MCP connection after upgrading to
discover the new tool. Staff can also explicitly complete a task with a handoff
summary from the queue.

### Pipes

A **pipe** is a named, ordered plan → execute → test pipeline on the same durable
queue (task #437 / GH #140). Open **Planning → Pipes** and choose **New pipe**.
Each stage names its own worker, optional agent/model, directory, instructions and
an optional **checkpoint after this stage**. Worker/model choices are validated
against that worker's reported catalog at creation; an offline worker or unknown
model/directory is refused before any row is written. Created pipes use
`POST /api/projects/{slug}/planning` with `operation: "pipe_create"`; approval and
stop use `pipe_approve` and `pipe_stop` with `pipe_id`.

Stages run one at a time as normal `PlanningJob` rows with a `pipe_stage`
reference. Only an explicit task `done` admits the next stage, and it is inserted
directly after the finished job so later queue work shifts down. The next stage's
first prompt includes every earlier stage artifact (up to 8,000 characters each)
plus any last failed test report, so execution receives the plan and test receives
the implementation without manual re-prompting. The completed task's result
summary is the artifact; its audit note and the pipe view expose it.

Failure policy is per pipe:

- **stop** (default) marks the stage and pipe failed, pauses the queue and waits
  for a human to resolve the task.
- **loop** (execute stage before a test stage) re-runs execute → test with a
  bounded `max_retries` (0–5, default 1). Each retry uses fresh stage cards and
  keeps the failed report as `last_failure` context; the cancelled retry job stays
  visible.

A checkpoint pauses the pipe with `awaiting_approval`; **Play** is refused until
**Approve checkpoint** (`pipe_approve`) queues the next stage. Stop pauses, cancels
waiting stage jobs and interrupts a running stage without marking its task done.

**Edit pipe** (`pipe_edit`) changes an active pipe's title, failure policy, retry
budget and its pending/queued stages: name, role, instructions, worker,
agent/model, directory and checkpoint. A queued stage's `PlanningJob` adopts the
new worker/snapshot and starts with it; a running job is never changed. Reorder,
add and remove apply only to the editable tail: completed and running stages stay
in place, and the stage already queued to run next can be edited or removed but
not reordered. **Remove pipe** (`pipe_remove`) refuses while a stage runs (Stop
first), cancels waiting stages/jobs and archives the pipe; history and audit stay
in the task/queue records, and the pipe drops out of the active Pipes list and
queue summary. Finished pipes can also be archived. Owner/CSRF/capability gates,
durable ordering and explicit-completion rules are unchanged; the generic queue
remove/reorder still refuses pipe stage jobs.


Planning API: staff-session-only `GET/POST /api/projects/{slug}/planning`, with
CSRF on writes and revision-based conflict detection. Read-only pipe inspection is
also available to MCP agents and signed-in viewers through
`GET /api/projects/{slug}/pipes` and `GET /api/projects/{slug}/pipes/{id}`; the
MCP tools `list_pipes` and `get_pipe` call those routes. Agent reporting uses
`POST /api/planning/jobs/{id}/report` plus agent authentication and the scoped
token. Architecture/operations and verification: [issue #28](https://github.com/aaronx3011/agents-task-manager/issues/28).
Apply migration `0026` before serving pipe editing/removal (0025 for pipes). Source:
`frontend/src/Planning.tsx`, `coordinator/planning_api.py`, and planning services
in `coordinator/services.py`; focused regression tests: `tests/test_planning.py`
and `tests/test_pipes.py`. Pipes need no Compose/env changes.

## Multi-AI chat rooms

A chat room is a persisted, project-scoped conversation in which several AI
participants, each holding a distinct **role** with its own persona and its own
model, read one shared history and answer in character. Open it at
`/?section=chatrooms`. Migration `0027` must be applied before serving it.

**Data model.** `ChatRoom` owns the room, the turn lease and the round-robin
queue. `ChatParticipant` is one role: a free-text `role` label, a `persona`
system prompt, a `{"providerID", "modelID"}` model and, for the worker kind, a
`Worker` plus OpenCode agent name. `ChatMessage` is one row per message with a
gapless server-owned `seq` that is also the read cursor. `ChatToolResult` is the
write-idempotency ledger. `ChatWorkerTurn` ships unused; see below.

**Roles, not pipeline stages.** `PipeStage.role` is a closed
`plan|execute|test|custom` label whose only effect is loop-retry policy, so it
cannot carry a character. A room participant therefore has its own `role` and
`persona`, and the server composes every prompt: a fixed preamble (stay in your
role, never speak as another participant), the room topic, that participant's
role and persona, the roster, and whether it may act. A topic, a persona and any
tool result are data, never instructions that can replace that preamble.

**One shared history, two points of view.** Each participant sees its own
earlier lines as its own turns and every other speaker's as labelled
`user` lines, merged so the provider sees an alternating conversation. The
prompt is windowed to `CHAT_ROOM_CONTEXT_MESSAGES`/`CHAT_ROOM_CONTEXT_BYTES`;
**the stored transcript is never pruned**, and reaching a hard room ceiling
refuses the next turn with 409 rather than deleting anything.

**Turn-taking.** `POST /api/v1/chat/rooms/{id}/messages` persists the human
message and fills the speaker queue (unmuted participants in `position` order,
once per round); `POST /api/v1/chat/rooms/{id}/advance` runs **exactly one**
speaker and returns. One provider call per request: six participants at the 45 s
provider timeout would otherwise be a 270 s HTTP request. Exclusivity is the
`VoiceConversation.busy_until` lease — the identity is the exact
`turn_busy_until` value, renewed before every provider call and every tool, with
lease-guarded writes and a conditional release — and the queue is durable, so a
crash mid-turn retries the speaker in flight instead of skipping it.
`POST .../advance {"reset": true}` recovers a stuck room.

**Exactly one participant may act.** Every other participant can read tasks,
pipes, the overview and **stored worker sessions and their messages**, but
cannot create or change anything. That is enforced three independent ways: a
partial unique constraint (`chat_room_single_actor`), the filtered tool schema,
and a `can_act` re-check read from the locked participant row — so a read-only
participant whose completion fabricates `create_task` is refused with nothing
written. The acting participant reuses only the mutations the PM voice agent
already exposes, each serializer-revalidated, fingerprinted against replay, and
**audited as the room's owner**: an acting room participant changes things with
its owner's authority, so the audit event names the room and the speaking role.

**Realtime.** A committed message emits one post-commit invalidation notice
carrying `room_id`; clients refetch the room with the `after` cursor they
already hold, so nothing message-sized crosses the broker and a broker outage
can never fail a committed message.

**Worker-backed participants are not implemented.** `kind=worker` validates and
stores, but advancing one returns 501 rather than quietly answering from the
provider. A `prompt` command returns no reply text (only `{"accepted": true}`),
dispatch happens solely inside that worker's own sync, and two concurrent worker
participants need two provisioned `Worker` + `Agent` pairs. Design and
acceptance criteria: [issue #150](https://github.com/aaronx3011/agents-task-manager/issues/150).

Plan, acceptance criteria and verification:
[issue #149](https://github.com/aaronx3011/agents-task-manager/issues/149).
Source: `coordinator/chat_rooms.py` (API, tool authorization),
`coordinator/chat_runtime.py` (turn lease, provider call),
`coordinator/chat_policy.py` (prompt, model allowlist, tool split),
`frontend/src/ChatRooms.tsx`; tests: `tests/test_chat_rooms.py`,
`tests/test_chat_room_tools.py`, `tests/test_chat_room_live.py`. Rooms reuse
`FIREWORKS_API_KEY` and need no Compose change; see the `CHAT_ROOM_*` variables
in `.env.example`.

## Notion mirror

The `notion-sync` service mirrors every project and task into the Notion
Project Management databases, one way: coordinator → Notion
([issue #94](https://github.com/aaronx3011/agents-task-manager/issues/94); two-way
edits are [issue #95](https://github.com/aaronx3011/agents-task-manager/issues/95)).
A task is pushed when its committed `updated_at` passes what was last mirrored, so
every lifecycle action reaches Notion within one pass (`NOTION_SYNC_INTERVAL_SECONDS`).
Heartbeat-only changes are pushed at most every `NOTION_HEARTBEAT_PUSH_SECONDS`.
Rows are matched by `Slug` and `Task ID`, so existing backfilled pages are adopted,
never duplicated. Owner, Due, Timeline and formula columns stay human-owned and are
never written. Notion errors back off per row (visible in Django Admin → Notion links)
and never fail a coordinator mutation.

Set `NOTION_TOKEN` to an internal Notion integration's secret (free on every plan),
share the Project Management page with that integration, and set the two data-source
IDs. `python manage.py sync_notion` runs one pass, `--loop` keeps running,
`--reconcile` reports and requeues drift (missing rows, wrong status/project,
duplicates, orphans) and `--force` re-pushes everything. Source:
`coordinator/notion.py`, `coordinator/notion_sync.py`; tests: `tests/test_notion_sync.py`.

## Source map

| Area | Files |
| --- | --- |
| Models and domain mutations | `coordinator/models.py`, `coordinator/services.py` |
| Read models and synchronization | `coordinator/queries.py`, `coordinator/sync.py`, `coordinator/live.py` |
| HTTP boundaries | `coordinator/urls.py`, `coordinator/views.py`, `coordinator/serializers.py` |
| Agent authentication | `coordinator/auth.py` |
| Dashboard reads and invalidation | `coordinator/dashboard_api.py`, `coordinator/realtime.py`, `config/asgi.py` |
| Accounts, invites and device tokens | `accounts/` |
| SPA front door | `deploy/web/Caddyfile`, `Dockerfile` (`web` target) |
| Frontend tooling | `frontend/package.json` |
| Agent registration | `frontend/src/Configuration.tsx`, `coordinator/views.py`, `coordinator/services.py` |
| Sessions shelf and Activity | `frontend/src/session-library.ts`, `frontend/src/ActivityPage.tsx` |
| Multi-AI chat rooms | `coordinator/chat_rooms.py`, `coordinator/chat_runtime.py`, `coordinator/chat_policy.py`, `frontend/src/ChatRooms.tsx` |
| Bridge configuration and calls | `bridge/settings.mjs`, `bridge/opencode.mjs` |
| Command recovery | `bridge/receipts.mjs` |
| Provisioning and maintenance | `coordinator/management/commands/` |
| Checks and integration harnesses | `tests/`, `bridge/bridge.test.mjs`, `bridge/smoke.mjs`, `scripts/` |

## Local development

Requires Python 3.12+, `uv`, and Node/npm for the frontend. The packaged bridge
tests require Node 20.19+. Run these commands from this project folder.

```sh
uv sync --locked
cp .env.example .env
# Configure .env for your local database, host, and optional Redis.
uv run python manage.py migrate
uv run python manage.py create_owner YOUR_NAME
uv run uvicorn config.asgi:application --reload
# In a second terminal: the SPA with a same-origin proxy to uvicorn.
npm ci --prefix frontend
npm run dev --prefix frontend
```

Visit http://localhost:5173/ for the workspace (sign in with the owner you created),
`/admin/` for Django administration (superusers only; see `DJANGO_ADMIN_ENABLED` and
`DJANGO_ADMIN_PATH` in `.env.example`), `/api/v1/schema` for the OpenAPI contract, and
`/healthz` for public liveness. Owners add people from **Users & invites** with
single-use invite links. `docker compose up` runs the same split: Django behind the
Caddy front door on http://localhost:8000. SQLite is
for solo development. PostgreSQL is the concurrent-agent database; Redis is
needed for notifications across multiple ASGI workers.

### Talk to the PM agent

Owners can open **Voice agent** in desktop/mobile navigation, or
`/?section=voice`. Hold **Push to talk** (or hold Space/Enter on the focused button), or type
in the composer. Spoken replies can be muted or interrupted; starting to speak
interrupts playback. Mention the project when asking the agent to create a task.
Ask for a task's status to get a fresh coordinator lookup. Tool results invalidate
the dashboard's task views. Conversation history is page-local and clears on
navigation/reload; **Clear conversation** also stops capture and playback and revokes
the backend conversation. Releasing the hold sends one voice turn; releasing outside
the button, Escape, Cancel recording, focus loss or hiding the page discards it.
Push-to-talk replaces automatic pause-to-send and **Send voice turn**; there is one
voice send path. A hold interrupts spoken replies, and acoustic barge-in remains available.
Hold for as long as you need; release inside to send exactly one turn, or cancel
to discard it. There is no client-side recording cutoff. The backend keeps one
generous abuse ceiling, configurable with `VOICE_MAX_CLIP_SECONDS` /
`VOICE_MAX_CLIP_BYTES` (defaults: 600 s and ~28.8 MB of mono 24 kHz PCM16).
Microphone access needs HTTPS or localhost.

The browser connects only to Django. **Fireworks AI** handles all PM reasoning
and function calling; Django owns the system prompt, bounded conversation history,
tool execution and audit writes. **ElevenLabs** handles both speech directions
through server-side calls: the `scribe_v1` speech-to-text API for microphone turns
and the text-to-speech API for spoken replies. There is no ElevenLabs provider
connection in the browser and no remote agent definition is used. Each short-lived
conversation token is bound to the owner's login; every tool call rechecks role,
session, expiry and revocation. Provider keys stay server-side. Typed chat needs
only Fireworks; voice additionally needs `ELEVENLABS_API_KEY` and a voice ID.
Text-to-voice switching preserves context.
The bounded backend transcript is cleared on revocation; expired grants cannot
be used. Microphone audio is converted to mono 24 kHz PCM16 WAV in the browser.
Django validates each clip against the configured ceiling, uploads it to
ElevenLabs speech-to-text, and takes the transcript. It never asks the speech
provider to generate a model reply.
Reply text is sent chunk-by-chunk to ElevenLabs text-to-speech, which returns MP3
audio for playback. Audio is not stored by Task Manager.
Task-status announcements are request-driven, not automatic background
monitoring.

The PM can also start work on the project's Planning queue (task #333). Say "launch
a new task in <project> to …", "launch task 42 on <worker> with <provider/model>",
"start the queue" or "play/resume the <project> queue". `launch_task` creates (or
reuses) the task, adds its planning card to the project's ordered queue and presses
Play; `play_queue` plays an existing queue; `get_queue` reads queue state, jobs,
workers and their reported models. These reuse the board's `enqueue`/`play`
operations, so the job shows on the Planning board, the task gets an audited note
by the signed-in owner, and execution still starts only when the worker syncs
(create/prompt worker commands). The reply names the task, worker, model, directory
and jobs ahead, and says "queued" rather than started or done. Requests are refused
with no change when the worker is offline, a named model is not in the worker's
reported catalog (or no catalog was reported), the queue is empty, or the task is
owned, terminal or has unfinished dependencies.

Queue corrections (task #336 / GH #111) use the same Planning mutation and dispatch paths:

- “Remove that queued task” → `remove_queued_job(project, job_id)` removes its waiting
  execution without cancelling the task. Even a cancelled task's stale queued job can
  be removed; running jobs are refused.
- “Switch that queued task to <worker> with <provider/model>” →
  `retarget_queued_job(project, job_id, worker?, model?, agent?)` edits the card and
  refreshes the existing queued snapshot, preserving order and frozen project context.
  At least one change is required. The selected worker must be online and named
  models/agents must be in its reported catalog. Running jobs cannot be retargeted.
- “Pause the <project> queue” → `pause_queue(project)` prevents the next launch;
  current work continues.
- “Stop the <project> queue” → `stop_queue(project)` pauses and enqueues the existing
  durable interrupt for current work. An interrupt is accepted/queued, not confirmed
  stopped or completed. Task ownership and busy-lease rules remain in force.

Use `get_queue` to resolve job IDs (distinct from task IDs). Replies include the
affected job/task, worker, agent, model, directory and resulting state; refusals
leave no partial mutation. Queue controls always recheck live state. Cancelling a
task atomically invalidates its queued execution; it does not silently interrupt
running work. Migration **0021** repairs old cancelled tasks' stale queued jobs with
audit notes; worker sync also defensively invalidates such entries before dispatch.
Only explicit task done advances successful work. Owner/capability checks apply to
every call, and foreign browser origins are refused. The PM cannot message worker
sessions. No Compose/env changes or new configuration are required.

Pipes are also in the PM toolset. `create_pipe(project, title, stages, on_failure?,
max_retries?, play?)` queues an ordered multi-stage pipeline; `get_queue` reports
each pipe's stages, workers, models and status; `approve_pipe(project, pipe_id)`
is the only way to resume a checkpoint; `stop_pipe(project, pipe_id)` pauses,
cancels waiting stages and may queue an interrupt. The router says accepted/queued,
never completed, and play is refused while a checkpoint waits.

Set these on the **Django API** container/service, not the frontend:

```sh
FIREWORKS_API_KEY=...             # or FIREWORKS_API_KEY_FILE=/secure/path
FIREWORKS_MODEL=accounts/fireworks/models/minimax-m3p1-flash
ELEVENLABS_API_KEY=...            # or ELEVENLABS_API_KEY_FILE=/secure/path
ELEVENLABS_VOICE_ID=...
ELEVENLABS_STT_MODEL=scribe_v3
ELEVENLABS_TTS_MODEL=eleven_v4_turbo
VOICE_PUBLIC_ORIGIN=https://your-public-task-manager-origin
VOICE_MAX_CLIP_SECONDS=600
VOICE_MAX_CLIP_BYTES=28801024
```

`VOICE_MAX_CLIP_SECONDS` and `VOICE_MAX_CLIP_BYTES` are a server-side abuse
ceiling, not a push-to-talk duration limit: the client never stops a held
recording. The defaults allow ten continuous minutes. Raise both together if a
longer hold is ever needed.

Do not use `VITE_*` variables for provider credentials. For local development,
`FIREWORKS_API_KEY_FILE` and `ELEVENLABS_API_KEY_FILE` can point at private key
files outside source control. `ELEVENLABS_VOICE_ID` must be a voice available to
that account; list candidates with the ElevenLabs voices API.

Apply migrations and restart Django after configuring providers. Choose a
Fireworks model/deployment that supports OpenAI-compatible function calling and
an ElevenLabs account/model that supports the speech API; both identifiers are
configurable. The earlier `LOCAL_SPEECH_ORIGIN`, `LOCAL_TTS_ORIGIN`, the Voice Lab
speech adapters and the old `configure_voice_agent` command are no longer used.
The PM policy and fourteen function schemas live in `coordinator/voice_policy.py`;
browser messages cannot replace the system prompt or supply conversation history.
PM completions explicitly send `reasoning_effort: "none"` to Fireworks to disable
extended thinking, including follow-up completions after tool results.
The page is owner-only because tool calls act with the owner's authority.
Provider calls are not automatically retried. A failed reply can follow a committed
tool action. The PM has no fixed reasoning-round or tool-call-count limit: it
continues until the model returns a final answer. Long turns renew their exclusive
conversation lease; access revocation, provider failures and context/payload
validation still apply. Recheck live state before repeating a mutation after an error.

Long replies are spoken in full: the browser splits at sentence/paragraph/word
boundaries into chunks of at most 900 characters, plays them in order, and
prefetches only the next chunk. Small batches reduce time to first audio without
reaching the provider's per-request limit. It never truncates a reply to fit one
request. Unbroken words/URLs and Unicode surrogate pairs are handled without
losing text. Mute, interruption, a new turn, End and Clear cancel queued
synthesis/playback. A failed chunk stops the queue and leaves the full text
visible; it is not retried automatically. Status badges indicate configuration,
not upstream health. Check the ElevenLabs API status if speech is unavailable.

Contract and browser verification:
[GitHub #109](https://github.com/aaronx3011/agents-task-manager/issues/109) and
`uv run --with playwright python scripts/verify_voice_agent_browser.py`.

### Register another agent

Sign in as an owner and open **Configuration → Agents**, or visit
`/?section=configuration` directly (also available in mobile navigation).

1. Enter a unique agent name and choose **Register agent**.
2. Copy the agent name and newly generated API key. The key is shown only in
   this registration panel; dismissing it, navigating away, or reloading removes it.
3. In **MCP connection**, enter the coordinator checkout path on that agent's
   machine. Copy the private environment variables and OpenCode MCP configuration.
4. Load the environment variables privately on the agent's machine. The JSON
   references `{env:COORDINATOR_API_KEY}` rather than embedding the raw key.
5. Restart the client and verify `get_overview`. The key resolves the registered
   agent name on the server; a separate agent-name environment variable is not needed.

The registered-agent list shows names, active status, and last-seen time. It
does not return existing keys, prefixes, or hashes. Duplicate names are rejected
without replacing the existing key or reactivating an inactive agent. Explicit
key rotation/deactivation remains available through existing administration.
Agent registration does not provision a worker bridge.

The configuration API is staff-session-only: `GET /api/agents?limit=&offset=`
returns `{count, items}`, and `POST /api/agents` with `{name}` returns the agent
metadata and a one-time `api_key` (201). Names are trimmed and limited to 100
characters; duplicates return 409. Responses use `Cache-Control: no-store`;
session writes require CSRF. Agent Bearer credentials cannot provision identities.
No new MCP tool or automatic client-configuration edit is added by this panel.

Agent-key creation and worker provisioning are explicit operator actions; see
[operations](https://github.com/aaronx3011/agents-task-manager/issues/3).
Configure the MCP process with `COORDINATOR_URL` (origin without `/api`) and
`COORDINATOR_API_KEY`. For a provisioned worker, use the variables and launch
instructions in [bridge/README.md](bridge/README.md). Default observation windows
are 50 sessions/30 messages at a 5-second persistence interval; counts alone do not
guarantee an upload fits the deployed HTTP body limit.

## Verification commands

```sh
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run pytest
npm run lint --prefix frontend
npm run format:check --prefix frontend
npm run build --prefix frontend
node --test bridge/bridge.test.mjs
node --test frontend/tests/session-live.test.mjs
node --test frontend/tests/*.test.mjs
# Built interface, fixture APIs and microphone: python scripts/verify_voice_agent_browser.py
# Built interface and fixture APIs: python scripts/verify_handoff_mode_browser.py
```

Use PostgreSQL to verify row-lock behavior. The bridge README describes live
smoke testing; those commands create sessions/commands and can invoke a model
when explicitly configured. This guide itself is not a live deployment report.

## Existing flow-improvement backlog

At this review, coordinator task **#83** / [GitHub #21](https://github.com/aaronx3011/agents-task-manager/issues/21)
tracks byte-bounded sync and HTTP 413 recovery. Task **#64** /
[GitHub #11](https://github.com/aaronx3011/agents-task-manager/issues/11) tracks
current buffered-event telemetry. Consult the live coordinator for their latest
status before starting implementation.
