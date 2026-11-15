#!/usr/bin/env bash
# Staging-like rehearsal of the 2-VPS production layout on one Docker host (#52).
#
# Brings up deploy/production-db (as "VPS2", bound to 127.0.0.1) and
# deploy/production (as "VPS1", Caddy TLS with its local CA) from freshly built
# images, then proves: HTTPS with HTTP->HTTPS redirect, the SPA and API through
# Caddy, an authenticated WebSocket (device token -> ticket -> wss), and a backup
# plus restore drill. Everything uses throwaway names and is removed at the end.
#
#   bash scripts/staging_rehearsal.sh        # needs Docker and uv
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
TAG="rehearsal"
SITE="atm.localhost"
HTTPS_PORT=18443
HTTP_PORT=18080
DB_PORT=55433
export IMAGE_TAG="$TAG"

secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
PG_PASSWORD="$(secret)"
OWNER_PASSWORD="Rehearsal-$(secret)"

app() {
  docker compose -p atm-rehearsal -f "$ROOT/deploy/production/compose.yaml" "$@"
}
db() {
  docker compose -p atm-rehearsal-db -f "$ROOT/deploy/production-db/compose.yaml" "$@"
}
cleanup() {
  echo "==> Removing rehearsal stacks"
  app down --volumes --remove-orphans >/dev/null 2>&1 || true
  db down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

export POSTGRES_PASSWORD="$PG_PASSWORD" DB_BIND_ADDRESS=127.0.0.1 DB_PORT
export BACKUP_DIR="$WORK/backups"
export DJANGO_SECRET_KEY="$(secret)" DB_HOST=host.docker.internal
export SITE_ADDRESS="$SITE" TLS=internal HTTP_PORT HTTPS_PORT

echo "==> Building images"
docker build -q --target api -t "agents-task-manager:$TAG" "$ROOT" >/dev/null
docker build -q --target web -t "agents-task-manager-web:$TAG" "$ROOT" >/dev/null

echo "==> VPS2: database + backup service"
mkdir -p "$BACKUP_DIR"
db up -d --wait --wait-timeout 120

echo "==> VPS1: Caddy (TLS) + Django + Redis"
app up -d --wait --wait-timeout 240

echo "==> Owner account"
app exec -T -e OWNER_PASSWORD="$OWNER_PASSWORD" web python manage.py create_owner rehearsal-owner --no-input

app exec -T front cat /data/caddy/pki/authorities/local/root.crt > "$WORK/root.crt"
CURL=(curl -fsS --max-time 10 --cacert "$WORK/root.crt" --resolve "$SITE:$HTTPS_PORT:127.0.0.1")
BASE="https://$SITE:$HTTPS_PORT"

echo "==> TLS checks"
"${CURL[@]}" "$BASE/healthz"; echo
redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "$SITE:$HTTP_PORT:127.0.0.1" "http://$SITE:$HTTP_PORT/")
echo "http -> $redirect"
[[ "$redirect" == 30[18]\ https://* ]]
"${CURL[@]}" -o /dev/null -w "SPA %{http_code} %{content_type}\n" "$BASE/"
"${CURL[@]}" -o /dev/null -w "HSTS %header{strict-transport-security}\n" "$BASE/"

echo "==> Device token, ticket and WebSocket over wss"
TOKEN=$("${CURL[@]}" -X POST "$BASE/api/v1/auth/tokens" -H 'Content-Type: application/json' \
  -d "{\"username\":\"rehearsal-owner\",\"password\":\"$OWNER_PASSWORD\",\"name\":\"rehearsal\"}" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
# Real rows for the restore drill to compare: a project and tasks via the API.
AUTH=(-H "Authorization: Token $TOKEN" -H 'Content-Type: application/json')
"${CURL[@]}" "${AUTH[@]}" -X POST "$BASE/api/v1/projects" \
  -d '{"slug":"rehearsal","name":"Rehearsal"}' -o /dev/null
for n in 1 2 3; do
  "${CURL[@]}" "${AUTH[@]}" -X POST "$BASE/api/v1/tasks" \
    -d "{\"title\":\"Rehearsal task $n\",\"project\":\"rehearsal\"}" -o /dev/null
done
echo "created a project and 3 tasks over HTTPS"
TICKET=$("${CURL[@]}" -X POST "$BASE/api/v1/realtime/ticket" -H "Authorization: Token $TOKEN" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["ticket"])')
uv run -q --with websockets python - "$SITE" "$HTTPS_PORT" "$WORK/root.crt" "$TICKET" <<'PY'
import asyncio, json, ssl, sys
import websockets

site, port, cafile, ticket = sys.argv[1:]
context = ssl.create_default_context(cafile=cafile)

async def main():
    # The URL carries the public name (Host, SNI); the socket goes to loopback.
    async with websockets.connect(
        f"wss://{site}:{port}/ws/dashboard/",
        ssl=context,
        host="127.0.0.1",
        port=int(port),
        subprotocols=["atm.v1", f"atm.ticket.{ticket}"],
    ) as socket:
        assert socket.subprotocol == "atm.v1", socket.subprotocol
        print("wss accepted:", json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "ping"}))
        print("wss reply:", json.loads(await socket.recv()))

asyncio.run(main())
PY

echo "==> Backup and restore drill on VPS2"
db exec -T backup sh /scripts/backup_postgres.sh
db exec -T backup sh /scripts/restore_drill.sh
ls -l "$BACKUP_DIR"
echo "REHEARSAL PASSED"
