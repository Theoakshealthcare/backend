#!/usr/bin/env bash
# Back up one Oakly stack: the Postgres database (custom-format dump) and the caregiver-documents volume.
#
# It works out the stack from its own location: it is shipped to <DEPLOY_PATH>/scripts/backup_cron.sh by the deploy, so
# <DEPLOY_PATH> holds docker-compose.prod.yml and .env, and the same script serves staging and production.
#
# Run by:
#   * scripts/remote_deploy.sh, before every migration (a failed backup stops the deploy);
#   * cron, daily 02:00 UTC (installed by the deploy):  <DEPLOY_PATH>/scripts/backup_cron.sh >> <DEPLOY_PATH>/backups/backup.log 2>&1
#   * by hand:  /opt/oakly-production/scripts/backup_cron.sh
#
# Output in <DEPLOY_PATH>/backups (override BACKUP_DIR), kept KEEP_DAYS days (default 14):
#   oakly-<UTC stamp>.dump              pg_dump custom format: restore with pg_restore (scripts/restore_test.sh proves it does)
#   private_uploads-<UTC stamp>.tgz     the KYC and licence photos (a Docker volume, not in Postgres)
#
# NOT offsite: the files sit on the same disk as the database. A lost server loses them too. Copy them elsewhere
# (docs/deployment/BACKUPS_GUIDE.md, "Offsite copy") before real patient data is stored.
set -euo pipefail

STACK_DIR="${STACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$STACK_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
ENV_FILE="$STACK_DIR/.env"
COMPOSE_FILE="$STACK_DIR/docker-compose.prod.yml"
DOCKER="$(command -v docker || echo /usr/bin/docker)"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

[ -f "$ENV_FILE" ] || { log "ERROR: $ENV_FILE not found"; exit 1; }
[ -f "$COMPOSE_FILE" ] || { log "ERROR: $COMPOSE_FILE not found"; exit 1; }

# Read single values from .env without sourcing it (a value may contain characters the shell would act on).
env_val() { grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true; }

PROJECT="$(env_val COMPOSE_PROJECT_NAME)"
PROJECT="${PROJECT:-oakly}"
UPLOADS_VOLUME="${UPLOADS_VOLUME:-${PROJECT}_private_uploads}"

compose() { "$DOCKER" compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"  # the dumps hold health data: only the owner reads them
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
partial="$BACKUP_DIR/.oakly-$stamp.dump.partial"
trap 'rm -f "$partial"' EXIT

log "[$PROJECT] database dump starting"
# -T: no TTY (cron has none). pg_dump runs inside the db container and streams to a file on the host.
compose exec -T db pg_dump -U oakly -d oakly --format=custom --no-owner > "$partial"

# A dump that cannot be listed is not a backup: fail loudly instead of keeping a truncated file.
compose exec -T db pg_restore --list < "$partial" > /dev/null

mv "$partial" "$BACKUP_DIR/oakly-$stamp.dump"
log "[$PROJECT] database dump: $BACKUP_DIR/oakly-$stamp.dump ($(du -h "$BACKUP_DIR/oakly-$stamp.dump" | cut -f1))"

# Caregiver documents (KYC and licence photos) live in a Docker volume, not in Postgres.
if "$DOCKER" volume inspect "$UPLOADS_VOLUME" > /dev/null 2>&1; then
  "$DOCKER" run --rm -v "$UPLOADS_VOLUME":/data:ro -v "$BACKUP_DIR":/out alpine:3.20 \
    tar czf "/out/private_uploads-$stamp.tgz" -C /data .
  log "[$PROJECT] uploads archive: $BACKUP_DIR/private_uploads-$stamp.tgz"
else
  log "[$PROJECT] no volume named $UPLOADS_VOLUME yet; uploads not archived"
fi

find "$BACKUP_DIR" -maxdepth 1 \( -name 'oakly-*.dump' -o -name 'private_uploads-*.tgz' \) -mtime +"$KEEP_DAYS" -delete
log "[$PROJECT] backup complete; kept $(find "$BACKUP_DIR" -maxdepth 1 -name 'oakly-*.dump' | wc -l | tr -d ' ') dump(s)"
