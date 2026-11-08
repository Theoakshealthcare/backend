#!/usr/bin/env bash
# Run `docker compose` for the Oakly staging stack with the right file and both env files.
#   /opt/oakly/deploy/compose.sh up -d
#   /opt/oakly/deploy/compose.sh ps
#   /opt/oakly/deploy/compose.sh logs -f --tail 100 api worker
#
# Layout it expects (override with the variables below):
#   $OAKLY_HOME/docker-compose.staging.yml   /opt/oakly = a git clone of the backend repo (branch staging)
#   $OAKLY_ENV_FILE_PATH                     /etc/oakly/staging.env  the app settings + the two passwords (not in git)
#   $OAKLY_IMAGE_ENV                         /etc/oakly/image.env    one line: OAKLY_API_IMAGE=<image>:<tag> (written by deploy.sh)
set -euo pipefail

OAKLY_HOME="${OAKLY_HOME:-/opt/oakly}"
OAKLY_ENV_FILE_PATH="${OAKLY_ENV_FILE_PATH:-/etc/oakly/staging.env}"
OAKLY_IMAGE_ENV="${OAKLY_IMAGE_ENV:-/etc/oakly/image.env}"
export OAKLY_ENV_FILE_PATH

args=(compose -f "$OAKLY_HOME/docker-compose.staging.yml" --env-file "$OAKLY_ENV_FILE_PATH")
if [ -f "$OAKLY_IMAGE_ENV" ]; then
  args+=(--env-file "$OAKLY_IMAGE_ENV")
fi

exec docker "${args[@]}" "$@"
