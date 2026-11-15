#!/usr/bin/env sh
# Regenerate the committed OpenAPI contract and the TypeScript client types from it.
# CI fails when either is stale (tests/test_openapi.py and `npm run api:check`).
set -eu
cd "$(dirname "$0")/.."
uv run python manage.py spectacular --format openapi-json --validate --fail-on-warn \
  --file openapi/schema.json
npm run api:generate --prefix frontend
