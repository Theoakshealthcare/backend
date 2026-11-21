# Backups and restore: Oakly API

Each stack (staging and production) backs up its own Postgres database and its caregiver-documents volume. One script serves both: `scripts/backup_cron.sh`, shipped by every deploy to `<DEPLOY_PATH>/scripts/`.

| What | Where | Kept |
|---|---|---|
| Database dump (`pg_dump` custom format) | `<DEPLOY_PATH>/backups/oakly-<UTC stamp>.dump` | 14 days (`KEEP_DAYS`) |
| Caregiver documents (KYC and licence photos, a Docker volume, **not** in Postgres) | `<DEPLOY_PATH>/backups/private_uploads-<UTC stamp>.tgz` | 14 days |

`<DEPLOY_PATH>` is `/opt/oakly-staging` or `/opt/oakly-production`. The backup directory is mode 700: the dumps hold health data.

## When it runs

| Trigger | Detail |
|---|---|
| **Before every migration** | `scripts/remote_deploy.sh` runs it after starting `db` and `redis`. If the backup fails, the deploy stops before touching the schema. This dump is the schema escape hatch, because rolling back an image does not roll back the database. |
| **Daily cron** | Installed by the first successful deploy: production at 02:00 UTC, staging at 02:15 UTC (so the two never run together). Log: `<DEPLOY_PATH>/backups/backup.log`. |
| **By hand** | `/opt/oakly-production/scripts/backup_cron.sh` |

A dump that cannot be listed with `pg_restore --list` is deleted and the script fails loudly, so a truncated file is never kept as a backup.

## Not offsite, yet

The backups sit on the same disk as the database. **A lost server loses them too.** Before real patient data is stored, copy them elsewhere: for example `rclone` to Backblaze B2 or an S3 bucket on a timer, with a retention of at least 30 days and an alert when the copy fails. This is an open follow-up (the owner chooses the provider); until it exists the backups protect against a bad migration or a mistaken delete, not against losing the server.

## Check that backups are happening

```bash
ls -lh /opt/oakly-production/backups/ | tail
tail -20 /opt/oakly-production/backups/backup.log
crontab -l | grep backup_cron
```

## Prove a backup restores (do this after the first backup, then monthly)

A backup is only a backup once a restore has been tried. This loads the newest dump into a scratch database next to the live one, checks it has the expected tables and an Alembic revision, and drops it. It never touches the live database.

```bash
/opt/oakly-production/scripts/restore_test.sh                      # newest dump
/opt/oakly-production/scripts/restore_test.sh /opt/oakly-production/backups/oakly-20261121T020000Z.dump
```

Expected last line: `restore test PASSED (scratch database dropped)`.

## Restore for real (a bad migration, or lost data)

Stop the app, restore into a fresh database, switch over. Read it through once before you need it.

```bash
cd /opt/oakly-production
C="docker compose -f docker-compose.prod.yml --env-file .env"
DUMP=backups/oakly-<stamp>.dump

# 1. take a safety copy of the CURRENT state first
scripts/backup_cron.sh

# 2. stop the writers
$C stop api worker

# 3. replace the database with the dump
$C exec -T db psql -U oakly -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE oakly" -c "CREATE DATABASE oakly"
$C exec -T db pg_restore -U oakly -d oakly --no-owner --exit-on-error < "$DUMP"

# 4. if a migration caused this, also roll the image back to the previous digest (ROLLBACK.md) BEFORE starting,
#    otherwise the old image will see the old schema but the new image would re-run the migration
$C up -d api worker
curl -sS http://127.0.0.1:8101/health      # staging: 8100
```

Caregiver documents: `tar xzf backups/private_uploads-<stamp>.tgz` into the volume with a throwaway container: `docker run --rm -v oakly-production_private_uploads:/data -v "$PWD/backups":/in alpine:3.20 tar xzf /in/private_uploads-<stamp>.tgz -C /data`.

## Retention and the encryption key

`KEEP_DAYS` (default 14) is read from the environment of the script: set it in the cron line if the owner wants longer. **The dumps contain health data encrypted with `VITALS_ENCRYPTION_KEY`**: a restore on a new server needs that same key (it is in the encrypted env file and the password manager). Lose the key and the encrypted columns are unreadable, backups or not.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ERROR: .../.env not found` | The stack has never been deployed, or the deploy path is wrong. |
| `docker compose ... service "db" is not running` | `docker compose -f docker-compose.prod.yml --env-file .env up -d db` |
| `Permission denied` running the script | `chmod +x /opt/oakly-production/scripts/*.sh` (a deploy does this) |
| Cron runs but the log is empty | `crontab -l` must show the absolute path of this stack; the directory `<DEPLOY_PATH>/backups` must exist. |
| Disk filling up | `du -sh <DEPLOY_PATH>/backups`; lower `KEEP_DAYS`, and check Docker's own usage with `docker system df`. |
