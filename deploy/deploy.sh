#!/usr/bin/env bash
# Deploy (or roll back) the staging API + worker to an image, with a backup and the migration as explicit steps.
#
#   deploy/deploy.sh --build oaklycare-backend:$(git rev-parse --short HEAD)   # first deploy: build on this server
#   deploy/deploy.sh ghcr.io/theoakshealthcare/backend:<git sha>               # pull a CI-built image
#   deploy/deploy.sh --rollback                                                # back to the image of the previous deploy
#
# Order (it stops at the first failure and says where):
#   1. record the image in /etc/oakly/image.env (the previous one is kept in image.env.prev)
#   2. build or pull
#   3. start db and redis, wait until healthy
#   4. pg_dump backup (skip with --skip-backup on the very first deploy, when the database is empty)
#   5. `alembic upgrade head` in a one-off container, so a bad migration stops the deploy BEFORE the new API serves traffic
#      (skipped on --rollback: an older image cannot run the newer database's migrations, and a rollback must not touch the schema)
#   6. start api and worker on the new image, wait for the API health check
#
# Rollback and migrations: the image goes back with --rollback, the schema does NOT. Most migrations here only add
# things and the older image keeps working; if one did not, restore the pre-deploy dump (docs/deploy/staging.md, section 12).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OAKLY_COMPOSE="${OAKLY_COMPOSE:-$HERE/compose.sh}"
OAKLY_IMAGE_ENV="${OAKLY_IMAGE_ENV:-/etc/oakly/image.env}"
export OAKLY_IMAGE_ENV
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-240}"

build=0
skip_backup=0
rollback=0
image=""
for arg in "$@"; do
  case "$arg" in
    --build) build=1 ;;
    --skip-backup) skip_backup=1 ;;
    --rollback) rollback=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) image="$arg" ;;
  esac
done

compose() { "$OAKLY_COMPOSE" "$@"; }
step() { printf '\n== %s\n' "$*"; }

if [ "$rollback" -eq 1 ]; then
  [ -f "$OAKLY_IMAGE_ENV.prev" ] || { echo "no previous image recorded ($OAKLY_IMAGE_ENV.prev)" >&2; exit 1; }
  image="$(sed -n 's/^OAKLY_API_IMAGE=//p' "$OAKLY_IMAGE_ENV.prev")"
  build=0
  skip_backup=1
  echo "rolling back to $image (the database schema is NOT rolled back)"
fi
[ -n "$image" ] || { echo "usage: deploy.sh [--build] [--skip-backup] <image> | --rollback" >&2; exit 2; }

step "1/6 record the image: $image"
if [ -f "$OAKLY_IMAGE_ENV" ]; then
  cp "$OAKLY_IMAGE_ENV" "$OAKLY_IMAGE_ENV.prev"
fi
printf 'OAKLY_API_IMAGE=%s\n' "$image" > "$OAKLY_IMAGE_ENV"

step "2/6 $([ "$build" -eq 1 ] && echo build || echo pull) the image"
if [ "$build" -eq 1 ]; then
  compose build api
elif ! compose pull api; then
  # A rollback to an image that was built on this server has no registry copy: use the local one if it is still there.
  docker image inspect "$image" > /dev/null 2>&1 || { echo "cannot pull $image and there is no local copy" >&2; exit 1; }
  echo "pull failed; using the local copy of $image"
fi

step "3/6 database and redis"
compose up -d --wait db redis

if [ "$skip_backup" -eq 0 ]; then
  step "4/6 backup before the migration"
  "$HERE/backup-db.sh"
else
  step "4/6 backup skipped"
fi

if [ "$rollback" -eq 1 ]; then
  # An older image cannot run `alembic upgrade head` on a database that is already ahead of it ("Can't locate
  # revision"), and a rollback must not touch the schema anyway.
  step "5/6 migrations skipped (rollback is image-only)"
else
  step "5/6 migrations (alembic upgrade head)"
  # -T: no TTY, so this also works from CI and cron.
  compose run --rm --no-deps -T api alembic upgrade head
fi

step "6/6 start api and worker"
compose up -d --remove-orphans api worker

api_container="$(compose ps -q api)"
deadline=$((SECONDS + HEALTH_WAIT_SECONDS))
while true; do
  state="$(docker inspect --format '{{.State.Health.Status}}' "$api_container" 2>/dev/null || echo unknown)"
  [ "$state" = "healthy" ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "api is not healthy after ${HEALTH_WAIT_SECONDS}s (state: $state). Last log lines:" >&2
    compose logs --tail 60 api >&2 || true
    echo "roll back with: $HERE/deploy.sh --rollback" >&2
    exit 1
  fi
  sleep 5
done

compose ps
echo
echo "deployed $image. Next: python3 scripts/staging_smoke.py https://api.<domain>"
