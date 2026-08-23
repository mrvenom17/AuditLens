#!/bin/sh
# Scheduled pg_dump (09_DEPLOYMENT.md § Database Deployment).
#
# "Backups: a scheduled pg_dump to a separate location, given this database
# holds professional audit records — this is not optional at this sensitivity
# level even at POC scale."
#
# Runs as a long-lived container rather than a host cron entry so the backup
# ships and starts with the application, instead of being a step someone has to
# remember on a new host.
#
# NOTE: /backups is a bind mount to the host. Getting it *off* this host is a
# separate step the operator must configure — see 09_DEPLOYMENT.md. A backup
# sitting on the same disk as the database it backs up protects against
# operator error, not against losing the disk.

set -eu

BACKUP_DIR=/backups
INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-86400}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

mkdir -p "$BACKUP_DIR"

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"
}

while true; do
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    target="$BACKUP_DIR/auditlens-$stamp.sql.gz"
    partial="$target.partial"

    log "starting dump -> $(basename "$target")"

    # Dump to a .partial name and rename on success. A backup file that exists
    # but is truncated is worse than no file, because it will be trusted.
    if pg_dump --format=plain --no-owner --no-privileges | gzip -9 > "$partial"; then
        mv "$partial" "$target"
        log "completed $(basename "$target") ($(wc -c < "$target") bytes)"
    else
        rm -f "$partial"
        log "FAILED - dump did not complete; previous backups are untouched"
    fi

    # Prune only fully-completed dumps, never .partial files, and only after a
    # successful run has been attempted.
    deleted=$(find "$BACKUP_DIR" -name 'auditlens-*.sql.gz' -type f -mtime "+$RETENTION_DAYS" -print -delete | wc -l)
    if [ "$deleted" -gt 0 ]; then
        log "pruned $deleted backup(s) older than $RETENTION_DAYS days"
    fi

    sleep "$INTERVAL_SECONDS"
done
