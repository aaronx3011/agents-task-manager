#!/usr/bin/env bash
# Bring up (or update) the production database host, VPS2 (#52). Run from the
# repository checkout on VPS2:  bash scripts/deploy_db.sh
# Idempotent; never deletes volumes or backups. Secrets live in
# deploy/production-db/.env (POSTGRES_PASSWORD, DB_BIND_ADDRESS), mode 600.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$BASE_DIR/deploy/production-db"
cd "$DEPLOY_DIR"
[[ -f .env ]] || { echo "Create $DEPLOY_DIR/.env first (see issue #3 runbook)" >&2; exit 1; }
chmod 600 .env

compose() { docker compose -f "$DEPLOY_DIR/compose.yaml" "$@"; }

set -a; source .env; set +a
case "${DB_BIND_ADDRESS:-}" in
  10.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|192.168.*|127.*) ;;
  *) echo "DB_BIND_ADDRESS must be a private address, got '${DB_BIND_ADDRESS:-}'" >&2; exit 1 ;;
esac

mkdir -p "${BACKUP_DIR:-./backups}" && chmod 700 "${BACKUP_DIR:-./backups}"
echo "==> Starting database and backup services"
compose up -d --wait --wait-timeout 300
echo "==> Taking a backup now and running the restore drill"
compose exec -T backup sh /scripts/backup_postgres.sh
compose exec -T backup sh /scripts/restore_drill.sh
compose ps
