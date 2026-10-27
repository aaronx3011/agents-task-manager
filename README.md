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
`/dashboard/?section=planning&project=PROJECT_SLUG`.

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
Combined first prompts are limited to the existing 20,000-character command bound.

The additional MCP tool **`report_planning_job`** accepts a job-specific token
provided in the first prompt. An authenticated agent can report heartbeat,
done/blocked/review for that active job even when its MCP identity differs from
the worker's bound agent. This delegates reporting only; it does not grant task
claims or access to other jobs. Task ownership stays with the bound agent and
worker sync never renews claims. Reload the MCP connection after upgrading to
discover the new tool. Staff can also explicitly complete a task with a handoff
summary from the queue.

Planning API: staff-session-only `GET/POST /api/projects/{slug}/planning`, with
CSRF on writes and revision-based conflict detection. Agent reporting uses
`POST /api/planning/jobs/{id}/report` plus agent authentication and the scoped
token. Architecture/operations and verification: [issue #28](https://github.com/aaronx3011/agents-task-manager/issues/28).
Apply migration `0011` before serving this feature. Source:
`frontend/src/Planning.tsx`, `coordinator/planning_api.py`, and planning services
in `coordinator/services.py`; focused regression tests: `tests/test_planning.py`.

## Source map

| Area | Files |
| --- | --- |
| Models and domain mutations | `coordinator/models.py`, `coordinator/services.py` |
| Read models and synchronization | `coordinator/queries.py`, `coordinator/sync.py`, `coordinator/live.py` |
| HTTP boundaries | `coordinator/urls.py`, `coordinator/views.py`, `coordinator/serializers.py` |
| Agent authentication | `coordinator/auth.py` |
| Dashboard and invalidation | `coordinator/dashboard.py`, `coordinator/realtime.py`, `config/asgi.py` |
| Frontend tooling | `frontend/package.json` |
| Agent registration | `frontend/src/Configuration.tsx`, `coordinator/views.py`, `coordinator/services.py` |
| Sessions shelf and Activity | `frontend/src/session-library.ts`, `frontend/src/ActivityPage.tsx` |
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
npm ci --prefix frontend
npm run build --prefix frontend
uv run python manage.py createsuperuser
uv run uvicorn config.asgi:application --reload
```

Visit `/dashboard/` for the workspace, `/admin/` for administration, `/api/` for
the authenticated browsable API, and `/healthz` for public liveness. SQLite is
for solo development. PostgreSQL is the concurrent-agent database; Redis is
needed for notifications across multiple ASGI workers.

### Register another agent

Sign in as staff and open **Configuration → Agents**, or visit
`/dashboard/?section=configuration` directly (also available in mobile navigation).

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
