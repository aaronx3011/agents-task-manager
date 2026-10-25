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

Checks: `node --test bridge/bridge.test.mjs` (Node 20.19+).
Live smoke: `node bridge/smoke.mjs` with `WORKER_A_ID`, `WORKER_A_KEY`,
`WORKER_B_ID`, `WORKER_B_KEY`, `COORDINATOR_URL`, `OPENCODE_URL` and
`SMOKE_DIRECTORY` set for two dedicated workers. Set `SMOKE_MODEL=providerID/modelID`
explicitly to also require a real assistant completion; this can invoke a provider.
Use `OPENCODE_A_URL` / `OPENCODE_B_URL` for separate local endpoints instead of
the shared `OPENCODE_URL`. Workspace scopes are canonicalized to match OpenCode.
The smoke leaves one deliberately uncertain command for staff reconciliation.
