# Deployment guide: two stacks on one VPS

The first-deploy steps (VPS, DNS, Docker, nginx, certificates, first admin, webhooks) are in the runbook `docs/deploy/staging.md`. This page is the map: how the two environments share one server, and the commands for operating them.

## The layout

One Ubuntu VPS (8 GB RAM, 4 vCPU), one nginx on the host, **two stacks** kept apart by directory, port, network and project name:

| | staging | production |
|---|---|---|
| Directory | `/opt/oakly-staging` | `/opt/oakly-production` |
| `COMPOSE_PROJECT_NAME` | `oakly-staging` | `oakly-production` |
| API on the host | `127.0.0.1:8100` | `127.0.0.1:8101` |
| Docker network | `172.29.0.0/24` | `172.29.1.0/24` |
| `TRUSTED_PROXIES` (same range) | `172.29.0.0/24` | `172.29.1.0/24` |
| Domain | `staging-api.<domain>` | `api.<domain>` |
| nginx file | `sites-available/oakly-api-staging` | `sites-available/oakly-api-production` |
| Backup cron | 02:15 UTC | 02:00 UTC |
| Branch that deploys | `develop` | `main` |

Each stack is `api`, `worker`, `db` (Postgres 16) and `redis`, from the one file `docker-compose.prod.yml`. Volumes are named after the project (`oakly-staging_postgres_data`, `oakly-production_private_uploads`, ...), so the two never share data. Only the API is published, on 127.0.0.1; Postgres and Redis have no host port.

What the deploy puts in a stack directory: `.env` (decrypted from `.env.<env>.enc`), `docker-compose.prod.yml`, `scripts/remote_deploy.sh`, `scripts/backup_cron.sh`, `scripts/restore_test.sh`, and `backups/`.

## Sizing (estimate, not measured)

| Piece | Memory |
|---|---|
| production: api (4 workers), worker, Postgres, Redis | about 2.5-3 GB |
| staging: api (`GUNICORN_WORKERS=3`), worker, Postgres, Redis | about 1.9-2.4 GB |
| nginx, Docker, OS | about 0.7 GB |
| **Total** | **about 5-6 GB** of 8 GB |

Measure with `docker stats --no-stream` after the first week and adjust. **Staging and production share one machine, so they share its failure modes**: a full disk, an out-of-memory kill or a lost server takes both down. Staging load tests can slow production. That is the accepted trade for one server (decision DEP-4); move production to its own server when real users depend on it.

## Security notes (accepted risks)

- **One server, one SSH user.** Both stacks are reached by the same `deploy` user, and that user is in the `docker` group, which is root-equivalent on the machine. Anyone holding a deploy secret for staging can in principle reach production's containers. The two Environments still hold separate secrets, branch rules and (for production) a required reviewer, and each can have its own SSH key; the shared server is the accepted price of DEP-4 (one VPS). Give production its own server when real users depend on it.
- **The registry token travels as an SSH environment variable** (`envs` of `appleboy/ssh-action`): it is the run's own short-lived, read-only-for-packages `GITHUB_TOKEN`, masked in logs. It is unverified whether that action puts it in the remote command line; check `ps` on the VPS during the first live deploy (WP7). If it shows, pipe the token over stdin instead (`printf %s "$TOKEN" | ssh ... 'docker login ghcr.io -u ... --password-stdin'`).
- **The server is pinned.** The deploy checks the VPS's host key against `VPS_SSH_FINGERPRINT` and refuses an unknown host.
- **"Verified" is a registry tag**, not a signature (`CICD_PIPELINE.md`, "Trust boundary"): people who can run workflows with the staging Environment's rights could mint one. Fine for a small team; signed attestations are the upgrade (SOP follow-up).

## Releasing

PRs go to `develop`; staging deploys on each merge; a release is a fast-forward of `main` (`BRANCHING.md`). The pipeline (`CICD_PIPELINE.md`) builds the image once and production runs the same bytes. Everything is off until the repository variables in `GITHUB_ACTIONS_SETUP.md` are set.

## Operating a stack

Replace the directory and port for production (`/opt/oakly-production`, 8101).

```bash
cd /opt/oakly-staging
C="docker compose -f docker-compose.prod.yml --env-file .env"

$C ps                                   # every service should be (healthy)
$C logs -f --tail 100 api worker        # follow the logs
curl -sS http://127.0.0.1:8100/health   # {"status":"healthy"}
grep '^API_IMAGE=' .env                 # the exact image digest running (not a secret)

$C exec db psql -U oakly -d oakly       # a database shell
scripts/backup_cron.sh                  # an extra backup now
scripts/restore_test.sh                 # prove the newest backup restores
```

Do **not** edit `.env` on the server: the next deploy overwrites it from the encrypted file. Change a setting by decrypting, editing and re-encrypting the file in git (`ENV_ENCRYPTION.md`), then deploy.

A one-off command in the app (for example creating the first Super Admin, `docs/deploy/staging.md` section 9.1):

```bash
$C exec api python -m scripts.create_super_admin --help
```

## Disk, logs and images

- Container logs rotate (10 MB x 5 per container). The registry copy of old images is pruned weekly (`registry-cleanup.yml`); the server prunes images older than 72 hours after each successful deploy.
- Check `df -h /` and `docker system df` monthly; the backups directory is the usual grower.

## What you cannot do from the pipeline

| Task | Who | Where |
|---|---|---|
| Create the VPS, DNS names, certificates | owner | `docs/deploy/staging.md` sections 2-6 |
| GitHub Environments, secrets, branch rules, the off-switch variables | owner | `GITHUB_ACTIONS_SETUP.md` |
| Rotate the leaked keys before any env file is committed | owner | `docs/sop/2026-10-05-secrets-and-rotation.md` |
| The first production release | owner's sign-off (DEP-5) | `BRANCHING.md` |
