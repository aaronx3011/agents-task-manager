# Repository instructions

Agent Task Coordinator is a Python 3.12+ / Django 5.2 service with Django REST Framework,
Django Admin, a staff-only Channels/WebSocket dashboard, and a stdio MCP HTTP adapter.
SQLite is for solo development; PostgreSQL is the concurrent-agent database.
Redis delivers dashboard notifications between ASGI workers.

## Agent skills

### Issue tracker
Use GitHub Issues in `aaronx3011/agents-task-manager` via `gh` for all specs, tickets,
progress, documentation, and follow-ups. Never create local Markdown work tickets.
Architecture/domain/API reference: https://github.com/aaronx3011/agents-task-manager/issues/1
Bootstrap implementation: https://github.com/aaronx3011/agents-task-manager/issues/2
Setup and operations: https://github.com/aaronx3011/agents-task-manager/issues/3
See `docs/agents/issue-tracker.md` for skill configuration.

### Triage labels
Use `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`.
See `docs/agents/triage-labels.md`.

### Domain docs
Single context. The glossary, decisions and stable API contract live in GitHub
documentation issues, not a local GLOSSARY.md or ADR directory.
See `docs/agents/domain.md` for the pointers and reading rules.

## Work coordination
Use the `task-coordination` and `task-reporting` skills when MCP is operational.
The service is the authority for atomic claims; GitHub Issues is the repository's
planning and documentation authority. Link them by URLs/task IDs.
During initial bootstrap, use issue #2 and explicitly report MCP unavailability.
Do not create or rotate credentials silently; raw agent keys must never be committed.
Do not commit or push unless requested by the user.

## Development
`uv sync --locked` installs dependencies. Copy `.env.example` to `.env` and configure
the environment. `uv run python manage.py migrate` initializes the database.
`npm ci --prefix frontend` and `npm run build --prefix frontend` build the React
workspace assets before collecting static files. Docker builds them automatically.
`uv run uvicorn config.asgi:application --reload` serves dashboard at `/dashboard/`,
admin at `/admin/` and API at `/api/`. WebSockets require this ASGI server.
Set `REDIS_URL` for multiple workers; the empty default is single-process memory only.
`uv run python manage.py createsuperuser` provisions a human admin.
`uv run python manage.py create_agent NAME` prints a new key once.

Checks: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run python manage.py check`, `uv run python manage.py makemigrations --check --dry-run`,
and `uv run pytest`. Run the suite against PostgreSQL for row-lock guarantees.
Frontend checks: `npm run lint --prefix frontend`, `npm run format:check --prefix frontend`,
and `npm run build --prefix frontend` (includes TypeScript checking).
Keep mutations/audit events transactional in `coordinator/services.py`; keep MCP
tools thin and HTTP-only. Never log raw API keys. Admin task edits must be audited.
Notify the dashboard only after transaction commit. Broker failures must not fail
an already committed API mutation. The disposable, loopback demo is started with
`uv run python scripts/start_dashboard_demo.py`; its tasks are driven by real MCP calls.
