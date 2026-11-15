# OpenCode worker bridge (issue #16)

A standalone Node worker that adapts the proven dashboard
`src/worker.mjs` / `src/opencode.mjs` bridge to the Django coordinator's
`POST /api/worker/sync` contract, without copying OpenCode itself.
Source reference: `open/opencode_repo/dashboard/src/{worker,opencode,command-receipts}.mjs`.
Run `node bridge/worker.mjs` from this checkout with the environment below.
Authoritative setup, compatibility and recovery instructions live in
[GitHub #3](https://github.com/aaronx3011/agents-task-manager/issues/3);
verification evidence lives in [#16](https://github.com/aaronx3011/agents-task-manager/issues/16).

## Files
- `opencode.mjs` — local HTTP/SSE client for the installed OpenCode v1 API
  (verified against 1.18.32) with the six command mappings.
- `receipts.mjs` — local command receipts: write-ahead intents, interrupted
  delivery becomes a one-shot *uncertain* error, never a blind replay.
- `worker.mjs` — the outbound-only sync loop (snapshots/updates/events/
  results/pending + command execution from the sync response).
- `live.mjs` — negotiated outbound WebSocket observations, 100 ms coalescing,
  bounded live snapshots, and immediate HTTP-sync wakeups.
- `smoke.mjs` — reproducible two-worker live harness (see below).

## Configuration (worker-scoped credentials, all local)
| Env | Meaning |
| --- | --- |
| `WORKER_ID` | The Django Worker.name registered via `manage.py provision_worker --agent AGENT` (required) |
| `COORDINATOR_API_KEY` | The **bound agent's** Bearer key (required; replaces DASHBOARD_TOKEN) |
| `COORDINATOR_URL` | e.g. `http://127.0.0.1:8321` (required) |
| `COORDINATOR_SYNC_PATH` | `/api/worker/sync` (default) |
| `OPENCODE_URL` | Local OpenCode server, default `http://127.0.0.1:4096` |
| `OPENCODE_DIRECTORY` | Restrict observation/execution to one workspace directory |
| `OPENCODE_CREATE_DIRECTORY` | Default directory for `create` commands |
| `WORKER_MODEL` | Default model as `providerID/modelID` |
| `WORKER_STATE_DIRECTORY` | Receipts state dir (defaults under `.runtime/workers/<scope>/`) |
| `SESSION_LIMIT` / `MESSAGE_LIMIT` / `POLL_INTERVAL_MS` | Bounded windows and persistence interval (50 / 30 / 5000) |
| `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | Local OpenCode basic auth if enabled |

The bridge never returns OpenCode or provider credentials to the server:
`meta.opencode_url` is the redacted endpoint, and only the coordinator's
worker-scoped agent key leaves the box.

## Live updates and batch persistence (#23)

The acknowledged HTTP sync advertises live protocol v1 before the bridge opens
`/ws/worker/` on the same coordinator origin. Node 22+ enables the native
WebSocket client; older runtimes retain HTTP-only operation. Authentication is
the first socket frame, never a URL parameter. Use HTTPS/WSS for remote workers.

OpenCode SSE events mark dirty sessions. At most once per 100 ms sampling pass,
the bridge reads the latest message window and sends replaceable full-state
overlays. Intermediate `message.updated`, `message.part.updated`, and
`message.part.delta` events are not individually stored in Django's event ring.
The database receives changed message windows through the normal HTTP batch,
every 5 seconds by default. Final/idle/error events, questions/permissions,
256 KiB of accumulated message events, command wakeups, and command results
request an earlier sync. There is still only one upload in flight, and a failed
batch retains its identical body and acknowledgement semantics.

Live frames are capped at 256 KiB and socket buffering at 512 KiB. An oversized
window signals a live gap rather than silently truncating messages; polling
remains the recovery path. The HTTP upload-size recovery backlog (#21) is
separate. Live delivery is ephemeral and does not acknowledge command execution
or durable message storage. Keep receipt files across restarts.

Only the browser's selected session subscribes to detailed live frames. Other
sessions remain available through database-backed overview polling. This first
version coalesces active-session observations worker-side without viewer-count
tracking; browser selection does not control OpenCode execution or collection.

Checks: `npm test --prefix bridge` (Node 20.19+; runs the OpenCode, Codex and Claude suites).
Live smoke: `node bridge/smoke.mjs` with `WORKER_A_ID`, `WORKER_A_KEY`,
`WORKER_B_ID`, `WORKER_B_KEY`, `COORDINATOR_URL`, `OPENCODE_URL` and
`SMOKE_DIRECTORY` set for two dedicated workers. Set `SMOKE_MODEL=providerID/modelID`
explicitly to also require a real assistant completion; this can invoke a provider.
Use `OPENCODE_A_URL` / `OPENCODE_B_URL` for separate local endpoints instead of
the shared `OPENCODE_URL`. Workspace scopes are canonicalized to match OpenCode.
The smoke leaves one deliberately uncertain command for staff reconciliation.

## Codex workers

Use a separate worker name, worker-bound agent key, and state directory for
Codex. Provision it with the existing coordinator worker command, then run
`node bridge/worker.mjs` with:

| Variable | Meaning |
| --- | --- |
| `WORKER_BACKEND=codex` | Use a local Codex App Server over stdio |
| `CODEX_BINARY` | Codex CLI executable, defaults to `codex` on PATH |
| `CODEX_WORKSPACE_ROOT` | Root containing allowed project directories |
| `CODEX_CREATE_DIRECTORY` | Default project directory for dashboard sessions |
| `CODEX_DIRECTORY` | Optional exact directory restriction |

Sign in to the Codex CLI as the OS user running the worker. The worker uses
its own persistent session registry inside `WORKER_STATE_DIRECTORY`. A
dashboard-created session starts a Codex thread when it receives its first
prompt. The dashboard supports streaming, subsequent turns, interruption and
command/file approval requests. Model IDs use the `openai/<model>` convention.
The worker reports only its own sessions, and refuses workspace paths outside
its configured local root. Retain the state directory when upgrading the
worker. Test the adapter with `node --test bridge/codex.test.mjs`.

## Claude Code workers (#98)

`WORKER_BACKEND=claude` drives local Claude Code sessions through the Claude
Agent SDK (Claude Code packaged as a library). Only this backend needs npm
packages: run `npm ci --prefix bridge` once. OpenCode and Codex workers keep
working without it. Provision a separate worker name, worker-bound agent key
and state directory, then run `node bridge/worker.mjs` with:

| Variable | Meaning |
| --- | --- |
| `WORKER_BACKEND=claude` | Use the Claude Agent SDK |
| `CLAUDE_CODE_OAUTH_TOKEN` | Subscription token from `claude setup-token` (see below) |
| `ANTHROPIC_API_KEY` | Alternative: Anthropic API key (or `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY` with that cloud's credentials) |
| `CLAUDE_WORKSPACE_ROOT` | Root containing allowed project directories |
| `CLAUDE_CREATE_DIRECTORY` | Default project directory for dashboard sessions |
| `CLAUDE_DIRECTORY` | Optional exact directory restriction |
| `CLAUDE_PERMISSION_MODE` | `default` (every gated tool asks the dashboard), `acceptEdits`, `plan`, `dontAsk` or `auto`. `bypassPermissions` is refused |
| `CLAUDE_CODE_EXECUTABLE` | Optional path to a Claude Code binary instead of the SDK's bundled one |

The worker does not reuse the Claude desktop app's login. To use a Claude
subscription, run `bridge/node_modules/@anthropic-ai/claude-agent-sdk-darwin-arm64/claude setup-token`
(or `claude setup-token` with the Claude Code CLI), sign in in the browser, and
export the printed token as `CLAUDE_CODE_OAUTH_TOKEN` for the worker only.
The Agent SDK docs document API keys and cloud providers; check Anthropic's
terms for your plan before relying on a subscription token. At startup it checks
the SDK's account info and reports "not signed in" on the dashboard instead
of accepting prompts. Credentials stay on the worker machine.

Each dashboard session is one streaming Agent SDK query that runs in its
project directory with the user's and project's Claude Code settings
(CLAUDE.md, MCP servers, permission rules). Follow-up prompts join the running
session; `interrupt` stops the current turn. Tool approvals and
`AskUserQuestion` appear as permission/question cards: `once` allows the call,
`always` also applies Claude Code's suggested session rule, and `reject` denies
it. Model IDs use `anthropic/<alias or model>` from the SDK's model list.
The registry in `WORKER_STATE_DIRECTORY/claude/` maps dashboard sessions to
Claude session IDs, so after a restart the next prompt resumes the same
conversation and history is reloaded from Claude Code's local transcript.
Test the adapter with `node --test bridge/claude.test.mjs`.
