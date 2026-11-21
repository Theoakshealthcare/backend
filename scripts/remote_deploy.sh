#!/usr/bin/env bash
# Deploy ONE stack (staging or production) on the VPS. Run by .github/actions/deploy-stack over SSH, from DEPLOY_PATH,
# after the action has copied .env, docker-compose.prod.yml and scripts/ there. Also safe to run by hand for a rollback.
#
# Inputs (environment variables):
#   IMAGE_REF         full image reference, normally name@sha256:<digest>            (required)
#   DEPLOY_PATH       the stack directory, e.g. /opt/oakly-staging                    (required)
#   STACK_ENV         staging | production (only used in messages and the cron time)  (default: stack)
#   GHCR_USER, GHCR_TOKEN   log in to ghcr.io before pulling; skipped when GHCR_TOKEN is empty
#   HEALTH_PATH       default /health
#   DEFAULT_API_PORT  port to probe when .env has no API_PORT, default 8000
#   INSTALL_CRON      1 (default) installs the daily backup cron; 0 skips it
#
# Order (it stops at the first failure and rolls back to the image that was serving):
#   1. record the running image          6. start api + worker on the new image and wait until every service is healthy
#   2. pull the new image by digest      7. probe the published port
#   3. pin API_IMAGE in .env             8. (success) install the backup cron, prune old images
#   4. start db + redis, take a backup
#   5. alembic upgrade head in a one-off container (a bad migration stops the deploy BEFORE the new API serves traffic)
#
# Rollback = code only. The schema is already at the new head and an older image cannot undo it: keep every migration
# backward-compatible for one release. The pre-migration dump in <DEPLOY_PATH>/backups is the schema escape hatch.
set -Eeuo pipefail

: "${IMAGE_REF:?IMAGE_REF is required}"
: "${DEPLOY_PATH:?DEPLOY_PATH is required}"
STACK_ENV="${STACK_ENV:-stack}"
HEALTH_PATH="${HEALTH_PATH:-/health}"
DEFAULT_API_PORT="${DEFAULT_API_PORT:-8000}"
INSTALL_CRON="${INSTALL_CRON:-1}"
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-240}"

cd "$DEPLOY_PATH"
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env)

step() { printf '\n==> [%s] %s\n' "$STACK_ENV" "$*"; }

# One value from .env, without sourcing the file.
env_val() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }

[ -f .env ] || { echo "::error::$DEPLOY_PATH/.env is missing" >&2; exit 1; }
[ -f docker-compose.prod.yml ] || { echo "::error::$DEPLOY_PATH/docker-compose.prod.yml is missing" >&2; exit 1; }

PROJECT="$(env_val COMPOSE_PROJECT_NAME)"
[ -n "$PROJECT" ] || { echo "::error::COMPOSE_PROJECT_NAME is not set in .env (oakly-staging or oakly-production)" >&2; exit 1; }
API_PORT="$(env_val API_PORT)"
HEALTH_URL="http://127.0.0.1:${API_PORT:-$DEFAULT_API_PORT}${HEALTH_PATH}"

pin_image() {
  if grep -qE '^API_IMAGE=' .env; then
    sed -i.bak "s|^API_IMAGE=.*|API_IMAGE=$1|" .env && rm -f .env.bak
  else
    printf 'API_IMAGE=%s\n' "$1" >> .env
  fi
}

probe_health() {
  local tries="$1" i
  for ((i = 0; i < tries; i++)); do
    if curl -fsS --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then return 0; fi
    sleep 3
  done
  return 1
}

step "1/7 Recording the running image (for rollback)"
# Found by compose's own labels, not `compose ps`: .env was just replaced, and `compose ps` would need to interpolate it.
PREV_CID="$(docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" --filter "label=com.docker.compose.service=api" | head -n 1)"
PREVIOUS_IMAGE=""
if [ -n "$PREV_CID" ]; then
  PREVIOUS_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$PREV_CID" 2> /dev/null || true)"
fi
echo "    previous: ${PREVIOUS_IMAGE:-<none: first deploy>}"
echo "    new     : $IMAGE_REF"

rollback() {
  trap - ERR
  echo "!!! [$STACK_ENV] DEPLOY FAILED (see the error above); rolling back to ${PREVIOUS_IMAGE:-<nothing recorded>}"
  if [ -n "$PREVIOUS_IMAGE" ]; then
    pin_image "$PREVIOUS_IMAGE"
    docker pull "$PREVIOUS_IMAGE" 2> /dev/null || true
    # --no-deps: do not touch db or redis. The schema may already be at the new head, so this rolls back code only.
    "${COMPOSE[@]}" up -d --no-deps api worker || true
    if probe_health 20; then echo "    rollback healthy"; else echo "    ROLLBACK DID NOT BECOME HEALTHY: look at the logs below"; fi
  else
    echo "    first deploy: nothing to roll back to. The stack is left as it is for inspection."
  fi
  echo "    newest backup(s): $(find backups -maxdepth 1 -name 'oakly-*.dump' 2> /dev/null | sort | tail -n 2 | tr '\n' ' ')"
  "${COMPOSE[@]}" logs --tail=80 api worker || true
  exit 1
}
trap rollback ERR

if [ -n "${GHCR_TOKEN:-}" ]; then
  step "2/7 Logging in to GHCR and pulling by digest"
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:?GHCR_USER is required with GHCR_TOKEN}" --password-stdin
  trap 'docker logout ghcr.io > /dev/null 2>&1 || true' EXIT
else
  step "2/7 Pulling by digest (no GHCR login: GHCR_TOKEN is empty)"
fi
docker pull "$IMAGE_REF"

step "3/7 Pinning API_IMAGE in .env"
pin_image "$IMAGE_REF"

step "4/7 Starting db and redis, then the pre-migration backup"
# Third-party images: refresh if the registry has newer patch builds (the API image is already pulled).
"${COMPOSE[@]}" pull db redis 2> /dev/null || true
"${COMPOSE[@]}" up -d --wait db redis
chmod +x scripts/backup_cron.sh
STACK_DIR="$DEPLOY_PATH" scripts/backup_cron.sh

step "5/7 Migrations (alembic upgrade head)"
# -T: no TTY over SSH. --no-deps: db and redis are already up.
"${COMPOSE[@]}" run --rm --no-deps -T api alembic upgrade head

step "6/7 Starting api and worker, waiting for every service to be healthy (up to ${HEALTH_WAIT_SECONDS}s)"
"${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout "$HEALTH_WAIT_SECONDS"

step "7/7 Probing $HEALTH_URL"
probe_health 10 || { echo "::error::[$STACK_ENV] the published port did not answer 200 on $HEALTH_URL"; false; }
trap - ERR
echo "==> [$STACK_ENV] deployed successfully: $IMAGE_REF"

if [ "$INSTALL_CRON" = "1" ]; then
  # Daily backup of this stack. Staging 02:15, production 02:00 so the two dumps never run together.
  minute=0
  [ "$STACK_ENV" = "production" ] || minute=15
  mkdir -p backups
  { crontab -l 2> /dev/null | grep -vF "${DEPLOY_PATH}/scripts/backup_cron.sh" || true
    echo "${minute} 2 * * * ${DEPLOY_PATH}/scripts/backup_cron.sh >> ${DEPLOY_PATH}/backups/backup.log 2>&1"
  } | crontab - && echo "Backup cron installed for ${DEPLOY_PATH}: daily at 02:${minute}" \
    || echo "::warning::could not install the backup cron: install it by hand (docs/deployment/BACKUPS_GUIDE.md)"
fi

# Keep images from the last 72 h so a manual rollback has them locally.
docker image prune -af --filter "until=72h" > /dev/null 2>&1 || true
