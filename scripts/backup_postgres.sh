#!/bin/sh
# Logical PostgreSQL backup with retention (#52). Runs in the postgres image on the
# database host (deploy/production-db/compose.yaml, service "backup").
#
#   backup_postgres.sh             take one backup now
#   backup_postgres.sh --schedule  take one backup every day at BACKUP_HOUR_UTC
#
# Uses the libpq environment (PGHOST, PGUSER, PGPASSWORD, PGDATABASE). Each dump is
# pg_dump custom format, checked with pg_restore --list before it counts, and dumps
# older than BACKUP_RETENTION_DAYS are removed. Nothing here prints the password.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
HOUR_UTC="${BACKUP_HOUR_UTC:-3}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

backup_once() {
  mkdir -p "$BACKUP_DIR"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  partial="$BACKUP_DIR/.${PGDATABASE}-${stamp}.dump.partial"
  final="$BACKUP_DIR/${PGDATABASE}-${stamp}.dump"
  pg_dump --format=custom --compress=6 --no-owner --file="$partial"
  # A dump only counts once its table of contents reads back.
  pg_restore --list "$partial" > /dev/null
  mv "$partial" "$final"
  chmod 600 "$final"
  log "wrote $(basename "$final") ($(du -h "$final" | cut -f1))"
  find "$BACKUP_DIR" -maxdepth 1 -name "${PGDATABASE}-*.dump" -type f \
    -mtime "+$RETENTION_DAYS" -print -exec rm -f {} \; | while read -r old; do
    log "removed expired $(basename "$old")"
  done
}

seconds_until_next_run() {
  now=$(date -u +%s)
  today=$(date -u +%Y-%m-%d)
  target=$(date -u -d "$today $(printf '%02d' "$HOUR_UTC"):00:00" +%s)
  if [ "$target" -le "$now" ]; then
    target=$((target + 86400))
  fi
  echo $((target - now))
}

if [ "${1:-}" = "--schedule" ]; then
  log "scheduled daily at ${HOUR_UTC}:00 UTC, keeping ${RETENTION_DAYS} days in ${BACKUP_DIR}"
  while true; do
    sleep "$(seconds_until_next_run)"
    backup_once || log "FAILED; will retry at the next scheduled time"
  done
else
  backup_once
fi
