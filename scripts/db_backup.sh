#!/bin/sh
# Daily compressed pg_dump with retention. Designed to run as the entrypoint of
# a long-lived sidecar container alongside postgres in docker-compose.
#
# Env vars:
#   PGHOST, PGUSER, PGPASSWORD, PGDATABASE  — passed straight to pg_dump
#   BACKUP_DIR                              — where to write dumps (default /backups)
#   RETENTION_DAYS                          — files older than this get deleted (default 14)
#   INTERVAL_SECONDS                        — seconds between dumps (default 86400 = 24h)
#
# Output filename pattern: wp2_YYYYMMDD_HHMMSSZ.sql.gz (UTC)
# Files are written via .tmp + atomic rename so a crash mid-dump never leaves
# a half-written .sql.gz that someone might try to restore.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-86400}"

mkdir -p "$BACKUP_DIR"

echo "[db_backup] starting (db=$PGDATABASE host=$PGHOST interval=${INTERVAL_SECONDS}s retention=${RETENTION_DAYS}d dir=$BACKUP_DIR)"

while true; do
  ts=$(date -u +%Y%m%d_%H%M%SZ)
  out="$BACKUP_DIR/${PGDATABASE}_${ts}.sql.gz"
  tmp="$out.tmp"

  echo "[db_backup] dumping → $out"
  if pg_dump --no-owner --no-acl --format=plain | gzip -9 > "$tmp"; then
    mv "$tmp" "$out"
    size=$(ls -lh "$out" | awk '{print $5}')
    echo "[db_backup] ok ($size)"
    # Rotation: drop dumps older than RETENTION_DAYS. -mtime +N means strictly
    # older than N*24h, so RETENTION_DAYS=14 keeps roughly the last 2 weeks.
    find "$BACKUP_DIR" -maxdepth 1 -name "${PGDATABASE}_*.sql.gz" -type f -mtime "+$RETENTION_DAYS" -print -delete || true
  else
    echo "[db_backup] FAILED — see pg_dump output above" >&2
    rm -f "$tmp"
  fi

  sleep "$INTERVAL_SECONDS"
done
