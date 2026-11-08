#!/usr/bin/env bash
# Prove a backup restores: load a dump into a scratch database next to the live one, check it, drop it.
#   deploy/restore-test.sh                      # the newest dump in $BACKUP_DIR
#   deploy/restore-test.sh /var/backups/oakly/oakly-20261110T020000Z.dump
# Never touches the live `oakly` database. Exit 0 = the dump restored and has the expected shape.
set -euo pipefail

OAKLY_COMPOSE="${OAKLY_COMPOSE:-/opt/oakly/deploy/compose.sh}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/oakly}"
SCRATCH_DB="oakly_restore_test"

dump="${1:-}"
if [ -z "$dump" ]; then
  dump="$(find "$BACKUP_DIR" -maxdepth 1 -name 'oakly-*.dump' | sort | tail -n 1)"
fi
if [ -z "$dump" ] || [ ! -s "$dump" ]; then
  echo "no dump found (looked in $BACKUP_DIR)" >&2
  exit 1
fi
echo "restoring $dump into scratch database $SCRATCH_DB"

psql_admin() { "$OAKLY_COMPOSE" exec -T db psql -U oakly -d postgres -v ON_ERROR_STOP=1 "$@"; }
psql_scratch() { "$OAKLY_COMPOSE" exec -T db psql -U oakly -d "$SCRATCH_DB" -v ON_ERROR_STOP=1 -At "$@"; }
cleanup() { psql_admin -q -c "DROP DATABASE IF EXISTS $SCRATCH_DB" > /dev/null 2>&1 || true; }
trap cleanup EXIT

psql_admin -q -c "DROP DATABASE IF EXISTS $SCRATCH_DB" -c "CREATE DATABASE $SCRATCH_DB"
"$OAKLY_COMPOSE" exec -T db pg_restore -U oakly -d "$SCRATCH_DB" --no-owner --exit-on-error < "$dump"

tables="$(psql_scratch -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
revision="$(psql_scratch -c "SELECT version_num FROM alembic_version")"
admins="$(psql_scratch -c "SELECT count(*) FROM admin_users")"
echo "restored: $tables tables, alembic revision $revision, $admins admin user(s)"

if [ "$tables" -lt 10 ] || [ -z "$revision" ]; then
  echo "restore looks wrong (too few tables or no alembic revision)" >&2
  exit 1
fi
echo "restore test PASSED (scratch database dropped)"
