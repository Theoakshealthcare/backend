# SOP: deployment pipeline adoption (work packages WP1-WP5)

| | |
|---|---|
| **What shipped** | Oakly now has the owner's build-once pipeline, hand-merged from `~/Documents/dev/backend-devops-template` (adopted at `c79bf6f`): a `develop` branch, one CI workflow with docs-only skipping and a `CI gate`, a staging CD workflow (build once, deploy by digest, smoke test, mark verified), a production CD workflow (promote the verified image, never build), one compose file for both environments on **one VPS**, a deploy script with a **pre-migration backup** and automatic rollback, per-environment encrypted env files (none committed yet), and the owner's GitHub setup guide. **Everything that deploys is switched off** until two repository variables are set. |
| **Branch / commits** | `develop` created on `oakly` at `1cf9c75` (WP1). Everything else on `ci/adopt-devops-template`, PR [#58](https://github.com/Theoakshealthcare/backend/pull/58) into **`develop`**, squash-merged 2026-11-21 as `91b9cbf`. Plan: [`docs/plans/2026-11-21-deployment-workflow.md`](../plans/2026-11-21-deployment-workflow.md); checklist section 18. |
| **No migration, no API change** | No endpoint or field changed, so there is no FE integration guide to update. One app-image change: `boot/docker-run.sh` reads `GUNICORN_WORKERS` (default 4, same as before). |
| **Not verified** | Anything that needs GitHub or a server: the workflows have never run on GitHub; the SSH, GHCR-token and crontab steps; two stacks side by side on one host; real nginx certificates. The first CI run (4 test shards, Semgrep, Trivy on the image) is the authority for those three and had not run when this was written. |

## Why
Deployments were a hand-run kit (`deploy/deploy.sh` on a git clone, secrets in `/etc/oakly`, a draft workflow, one 45-60 minute CI job, no `develop`). The owner chose the template's model: build the image once on `develop`, test that image on staging, and redeploy those exact bytes to production. All five decisions were answered 2026-11-21 (plan section 3): DEP-1 two branches, DEP-2 encrypted env files in git with one key per environment, DEP-3 the repo is private, DEP-4 one VPS for both environments, DEP-5 the first production release waits for sign-off and the live passes.

## How
The template was installed into a scratch copy and into the worktree **without `--force`** (flags `--slug oakly --name "Oakly API" --health-path /health --py-version 3.13 --poetry-version 2.2.1`), then hand-merged. Decisions and their reasons:

| Decision | Why | Rejected |
|---|---|---|
| **Off-switches**: every CD job needs `STAGING_DEPLOY_ENABLED=true` (staging) or `PRODUCTION_DEPLOY_ENABLED=true` (production) | Merging the workflows to `develop` or `main` must not deploy, or fail loudly, before the VPS, secrets and sign-off exist. DEP-5 needs a production switch that only the owner flips. | The template as shipped (a push to `main` would try to deploy and go red) |
| **One compose file** `docker-compose.prod.yml`; project name, API port, subnet, Redis memory come from `.env`; `image: ${API_IMAGE}`, never `build:` | Two stacks on one host must differ only by variables; production must never build on the server. The old `docker-compose.staging.yml`, the managed-database production file, `deploy.sh` and `compose.sh` are removed. | Two compose files (they drift) |
| **Deploy logic moved out of the action's inline YAML into `scripts/remote_deploy.sh`** | It can be run locally against a scratch stack and `shellcheck`ed; the YAML was untestable. | Inline SSH script |
| **Pre-migration backup** in `remote_deploy.sh`, before `alembic upgrade head`; a failed backup stops the deploy | The template only had a 02:00 cron. Our old `deploy.sh` had this safety; rolling an image back does not roll the schema back. | Backup only by cron |
| **Backup scripts replaced with ours** (`scripts/backup_cron.sh`, `scripts/restore_test.sh`); the template's four backup scripts removed | The template's scripts name the container `${COMPOSE_PROJECT_NAME}_db`, but Compose v2 names it `<project>-db-1`, so they could not find the database; they also skip the caregiver-documents volume, and they hard-code one deploy path. Ours is stack-relative, dumps in custom format, checks the dump lists, archives the uploads volume. | Patching four template scripts |
| **Previous image found by Compose labels**, not `docker compose ps` | The new `.env` is already in place when the deploy records the old image, and `compose ps` must interpolate `${API_IMAGE:?}`. If that failed the old image was never recorded, so a failed deploy had nothing to roll back to. Found while reading; the scratch test confirmed the label lookup works. | Template's `compose ps -q api` |
| **Health gate is `up -d --wait`** (api, worker, db, redis all healthy), and rollback restores `api` **and** `worker` | A worker that crashes on the new image stops payouts and dispatch silently; the template only watched the API and rolled back only the API. | API-only check |
| **Per-environment encryption keys** (`.env.key.<env>`, `scripts/env.sh`); `env.sh diff` prints names only; the action refuses an env file whose `COMPOSE_PROJECT_NAME` is not `oakly-<env>` and any `DEPLOY_PATH` other than the two known | DEP-2 says one key per environment. The checks make it hard to ship the wrong file to the wrong stack on a shared box. | The template's single shared key |
| **CI keeps our checks** (bug-class ruff, `ci_smoke`, the eight FE-guide `--check` runs, testcontainers suite) and takes the template's structure (docs-only skip, draft skip, cancel superseded, `CI gate`, Semgrep, Trivy on PRs) | Black/`ruff app/` are not enforced yet: the codebase is not clean. `tests/` in the template is `app/tests` here. | The template's lint and `tests/` job |
| **Tests in 4 shards** via `pytest-split` (a dev-only dependency, `poetry.lock` +17 lines) | Wall time. **Billed minutes do not fall** (repeated setup): on a private repo a code PR costs about an hour of Actions minutes. | Hand-rolled file splitting; no sharding |
| **Staging "E2E" is `scripts/staging_smoke.py`** run without `--email`: health, OpenAPI, both webhooks refuse unsigned calls, pricing preview | These need no person or mailbox. Email, payment link and push token need a person and stay manual. A fresh staging **fails the pricing check by design** until the owner publishes the one-time price row (no prices are ever auto-seeded). | Skipping E2E; inventing a seed price |
| **No seed hook, no rate-limit key flush** (both in the template) | We never auto-seed; the flush used another app's key pattern (`http_*`). | |
| **GUNICORN_WORKERS** read by `boot/docker-run.sh` (default 4) | Lets staging run 2 workers so two stacks fit on 8 GB. | Editing the Dockerfile CMD |
| **PRs go to `develop`; `develop` should be the default branch**; `git push oakly oakly/develop:main` is the release | CI triggers on PRs to `develop` only; a PR against `main` gets no checks. The remote is `oakly`; `origin` is the old `innocent98` repo. | |

## What's involved
| Area | Paths |
|---|---|
| Workflows and action | `.github/workflows/ci.yml` (merged), `cd-staging.yml`, `cd-production.yml`, `registry-cleanup.yml`, `.github/actions/deploy-stack/action.yml`, `.github/dependabot.yml`; removed `deploy-staging.yml` (the 2026-11 draft) |
| Stack | `docker-compose.prod.yml` (replaces the old prod and staging files), `deploy/env.example` (was `deploy/staging.env.example`), `deploy/nginx/oakly-api.conf` (now a per-environment template), `deploy/nginx/oakly-common.conf` (new, catch-all servers) |
| Scripts | `scripts/remote_deploy.sh`, `scripts/backup_cron.sh`, `scripts/restore_test.sh` (moved from `deploy/`), `scripts/env.sh`, `scripts/ghcr_digest.sh`, `scripts/ghcr_prune.sh`, `scripts/security-scan.sh`; removed `deploy/deploy.sh`, `deploy/compose.sh` |
| Docs | `docs/deployment/` (8 guides, all tailored), `docs/deploy/staging.md` (runbook: sections 0-2, 5-8, 12, 14-16 rewritten), plan section 3 and WP table, checklist 16 and 18; supersede notes on the 2026-11-08 staging-kit SOP and two older SOPs that named `/etc/oakly` |
| Code and config | `boot/docker-run.sh:83` (`GUNICORN_WORKERS`), `.gitignore` (allows `.env.staging.enc`, `.env.production.enc`), `pyproject.toml` / `poetry.lock` (`pytest-split`), `app/tests/test_staging_env_example.py` (follows the renamed file; 3 new tests) |
| Git | `oakly/develop` created at `1cf9c75` |

## Verification
Everything that could be run without GitHub or a server was run. **Nothing here was run on a server.**

| Check | Result |
|---|---|
| `actionlint .github/workflows/*.yml` | clean |
| `shellcheck` on `remote_deploy.sh`, `backup_cron.sh`, `restore_test.sh`, `env.sh` | clean |
| Every pinned action SHA | exists upstream (`gh api repos/<action>/commits/<sha>`, 11 actions) |
| `app/tests/test_staging_env_example.py` | 9 passed (the only test file touched) |
| `nginx -t` with both sites rendered, throwaway certificates, `nginx:alpine` | ok (two harmless `http2` warnings) |
| `scripts/env.sh` on throwaway data | encrypt, verify, decrypt, wrong key refused, `diff` shows names only |
| **Scratch stack** (local Docker, local `registry:2`, throwaway keys, all removed afterwards) | First deploy: boot, migrations to `0027_booking_minutes`, all services healthy. Redeploy of the same digest: previous image found by labels, post-migration dump 212 KB plus uploads archive. `restore_test.sh`: 70 tables, revision 0027, scratch database dropped. **Broken image** (exits at start): deploy failed, `API_IMAGE` back to the previous digest, `/health` 200, exit code 1, newest backups named. |
| `scripts/staging_smoke.py` against the scratch stack | 5 PASS and exit 0 with a fake pricing row inserted into the scratch DB; with no pricing row: 4 PASS, the pricing check FAILS with 503 `booking_settings_not_configured` |
| CI on the PR (4 shards, Semgrep, Trivy, `CI gate`) | **all green on head `af20116`** after two fixes it found: Semgrep flagged constant-only SQL in migrations 0014/0015 (suppressed, comment only); Trivy found 12 fixable CVEs (`certifi`, `dnspython`, `pyasn1` bumped in the lock; pip removed from the runtime image). Shards took 10-17 minutes wall. |

**Not verified:** the workflows on GitHub; `appleboy/scp-action` and `ssh-action` behaviour (including that scp creates nested `scripts/`); the GHCR login with the run's token; the crontab step; two stacks running together (ports, subnets, volumes are checked only by tests and `compose config`); real certificates; the test shards being order-independent (the suite had always run in one process); whether Semgrep and Trivy are green on today's code.

## Operate / roll back
- **Turn on:** follow [`docs/deployment/GITHUB_ACTIONS_SETUP.md`](../deployment/GITHUB_ACTIONS_SETUP.md) (owner clicks, WP6), then the runbook's sections 1-9. Staging switch: repository variable `STAGING_DEPLOY_ENABLED=true`. Production switch: `PRODUCTION_DEPLOY_ENABLED=true`, only after sign-off (DEP-5).
- **Release:** `git fetch oakly && git push oakly oakly/develop:main` (fast-forward only).
- **Roll back a deploy:** automatic on failure; manual by re-running CD with `image_tag` ([`ROLLBACK.md`](../deployment/ROLLBACK.md)); schema escape hatch is the pre-migration dump in `<DEPLOY_PATH>/backups` ([`BACKUPS_GUIDE.md`](../deployment/BACKUPS_GUIDE.md)).
- **Roll back this change:** revert the PR on `develop`. Nothing deploys today, so reverting changes only CI and the files; the old kit files return with it. `develop` itself is harmless to leave.

## Follow-ups
1. **Owner clicks (WP6):** Environments, secrets, branch rules, default branch to `develop`, the off-switch variables; delete the stale 2025 `oakly/staging` remote branch (a remote delete the lead cannot run).
2. **Rotate the leaked keys before committing any `.env.*.enc`** (Paystack, Zoho, Gemini, Cloudinary, the pasted Prembly key).
3. **WP7: the first live run** on the VPS: runbook sections 1-9, then merge to `develop`, watch staging, exercise rollback once.
4. **Read the PR's first CI run:** Semgrep and Trivy were never run on this code (a Semgrep JWT-decode finding in `app/api/deps.py` is already known). Add `.trivyignore` entries or fix; commit a `.test_durations` file from a `--store-durations` run to balance the shards by time.
5. **Billed minutes:** about an hour per code PR; check the organisation plan.
6. **Off-server backups** and a failure alert (the backups share the database's disk).
7. **Feed three fixes back to the template repo:** the container-name assumption in its backup scripts, previous-image detection via `compose ps`, and the single worker/API health gate.
8. A secrets scan (gitleaks) in CI is not in the template; consider it now that the repo holds encrypted env files.
9. **Security review notes (2026-11-21):** the SSH host key is now pinned (`VPS_SSH_FINGERPRINT`, fixed after an automated review); the shared `deploy` user and the forgeable `staging-verified-*` registry tag are accepted for now (DEP-4, `CICD_PIPELINE.md` trust boundary). Upgrade path: sign verified images with cosign keyless signing bound to the `cd-staging` workflow on `develop`, verify in `cd-production`; per-environment SSH keys; production on its own server.
10. Production-only: set `GUNICORN_WORKERS` and `REDIS_MAXMEMORY` from measurements after the first week.
