#!/usr/bin/env bash
# Back up the staging database (and the caregiver-documents volume) to $BACKUP_DIR, keep $KEEP_DAYS days.
# Run by cron (docs/deploy/staging.md, section 8) and by deploy.sh before every migration.
# The dump is Postgres custom format: restore with pg_restore (see deploy/restore-test.sh).
# A backup is only a backup once a restore has been tried: run restore-test.sh after the first one.
set -euo pipefail

OAKLY_COMPOSE="${OAKLY_COMPOSE:-/opt/oakly/deploy/compose.sh}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/oakly}"
KEEP_DAYS="${KEEP_DAYS:-14}"
UPLOADS_VOLUME="${UPLOADS_VOLUME:-oakly-staging_private_uploads}"

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"  # the dumps hold health data: only the owner reads them
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
partial="$BACKUP_DIR/.oakly-$stamp.dump.partial"
trap 'rm -f "$partial"' EXIT

# -T: no TTY (cron has none). pg_dump runs inside the db container and streams to a file on the host.
"$OAKLY_COMPOSE" exec -T db pg_dump -U oakly -d oakly --format=custom --no-owner > "$partial"

# A dump that cannot be listed is not a backup: fail loudly instead of keeping a truncated file.
"$OAKLY_COMPOSE" exec -T db pg_restore --list < "$partial" > /dev/null

mv "$partial" "$BACKUP_DIR/oakly-$stamp.dump"
echo "database dump: $BACKUP_DIR/oakly-$stamp.dump"

# Caregiver documents (KYC and licence photos) live in a Docker volume, not in Postgres.
if docker volume inspect "$UPLOADS_VOLUME" > /dev/null 2>&1; then
  docker run --rm -v "$UPLOADS_VOLUME":/data:ro -v "$BACKUP_DIR":/out alpine:3.20 \
    tar czf "/out/private_uploads-$stamp.tgz" -C /data .
  echo "uploads archive: $BACKUP_DIR/private_uploads-$stamp.tgz"
fi

find "$BACKUP_DIR" -maxdepth 1 \( -name 'oakly-*.dump' -o -name 'private_uploads-*.tgz' \) -mtime +"$KEEP_DAYS" -delete
