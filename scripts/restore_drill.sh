#!/bin/sh
# Restore drill (#52): prove the newest backup restores, without touching the live
# database. Restores into a scratch database on the same server, compares row
# counts of the key tables with the live database, then drops the scratch copy.
#
# Run inside the backup container on the database host:
#   docker compose -f deploy/production-db/compose.yaml exec backup sh /scripts/restore_drill.sh
# Optional: pass a dump path to drill a specific file.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
LIVE="${PGDATABASE:-coordinator}"
SCRATCH="${LIVE}_restore_drill"
DUMP="${1:-$(ls -1t "$BACKUP_DIR"/"$LIVE"-*.dump 2>/dev/null | head -n 1)}"

log() { echo "restore-drill: $*"; }

[ -n "$DUMP" ] && [ -f "$DUMP" ] || { log "no dump found in $BACKUP_DIR" >&2; exit 1; }
log "restoring $(basename "$DUMP") into scratch database $SCRATCH"

dropdb --if-exists "$SCRATCH"
createdb "$SCRATCH"
trap 'dropdb --if-exists "$SCRATCH" >/dev/null 2>&1 || true' EXIT
pg_restore --no-owner --exit-on-error --dbname="$SCRATCH" "$DUMP"

status=0
for table in coordinator_task coordinator_taskevent coordinator_project coordinator_agent \
  auth_user accounts_userprofile django_migrations; do
  restored=$(psql -d "$SCRATCH" -Atc "select count(*) from $table")
  live=$(psql -d "$LIVE" -Atc "select count(*) from $table")
  # The live database may have grown since the dump; it must never have fewer rows
  # restored than existed in the dump, and an empty restore of a non-empty table fails.
  if [ "$restored" -gt "$live" ] || { [ "$restored" -eq 0 ] && [ "$live" -gt 0 ]; }; then
    log "MISMATCH $table restored=$restored live=$live"
    status=1
  else
    log "ok $table restored=$restored live=$live"
  fi
done

latest=$(psql -d "$SCRATCH" -Atc "select app || '.' || name from django_migrations order by id desc limit 1")
log "latest migration in dump: $latest"
[ "$status" -eq 0 ] && log "PASS" || log "FAIL"
exit "$status"
