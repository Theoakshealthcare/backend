# Deployment workflow: adopt `backend-devops-template`

**Status: plan only, nothing installed, nothing changed in CI.** Written 2026-11-21 by the lead session. The template lives at `~/Documents/dev/backend-devops-template` (commit `76a5b04`, SOP `docs/sop/2026-11-18-build-once-promote-pipeline.md` in that repo). Its own checklist says it has **never run on a real repo and VPS**, so Oakly's first deploy is also the template's first live test.

## 1. The model we are adopting

**Build once, test that image, ship that image.** A push to `develop` builds the image one time, pushes it to GHCR, deploys it to staging by digest, runs the live E2E, and only then tags it `staging-verified-<sha>`. A push to `main` (a fast-forward of `develop`) deploys that exact image to production. Production never builds. A failed deploy restores the previous image by itself. Rollback is a manual run naming an already-verified tag. Docs-only and draft PRs skip the heavy jobs; superseded PR runs cancel; one required check named `CI gate`.

## 2. How Oakly differs from what the template assumes

| Template assumes | Oakly today | What to do |
|---|---|---|
| Branches `develop` (staging) and `main` (production, linear history) | `main` only. `staging` on the remote is a stale branch from 2025 (`f75c2b2`). No branch protection on any branch. | Create `develop` from `main`. Decision DEP-1. |
| Health path `/api/v1/health`, Python 3.11, Poetry 1.8.4 | `/health`, Python **3.13** (do not use 3.14: `arq` breaks), Poetry **2.2.1** | Install with `--health-path /health --py-version 3.13 --poetry-version 2.2.1 --slug oakly --name "Oakly API"`. |
| One `docker-compose.prod.yml` with `api` on `image: ${API_IMAGE}`, `.env` shipped next to it | `docker-compose.prod.yml` assumes a **managed database** and uses `OAKLY_API_IMAGE` and `/etc/oakly/backend.env`; `docker-compose.staging.yml` (db, redis, api, worker) is the one we wrote for the VPS | One stack file for both environments: db, redis, api, worker, `API_IMAGE`. Retire the two old files. |
| `.env.<env>.enc` (AES-256) committed in git, key in a GitHub secret | Env lives **on the server** (`/etc/oakly/*.env`), never in git; the kit's runbook fills it with `openssl rand` so no value is printed | Decision DEP-2. **The repo is public.** |
| Deploy script: pull by digest, `alembic upgrade head`, `up -d`, health, auto-rollback | `deploy/deploy.sh` does the same and **also takes a `pg_dump` before the migration** | Keep the template's flow, **add the pre-migration backup step** to the shared action (the template has only a 02:00 cron on production). |
| Own CI with Postgres service containers and `pytest tests/` | CI runs ruff (bug-class), `scripts.ci_smoke`, eight FE-guide `--check` runs, then `pytest` with testcontainers on `app/tests`. One job, **45-60 minutes**, no sharding. | **Keep our checks**; take only the template's structure (change filter, draft skip, cancel superseded, `CI gate`, Trivy image scan). Shard the tests (`pytest-split`). |
| Live E2E in `tests/e2e` marked `e2e` (skipped if absent) | None. We have `scripts/staging_smoke.py` (health, webhooks, pricing, email code, login, Paystack link, push token) | Run the smoke script as the staging E2E; the image is marked verified only if it passes. |
| Alembic baked into the image | Migrations are under `app/` and `alembic.ini` is copied, so they are in the image | Verify `docker compose run --rm api alembic upgrade head` on a scratch stack (the Docker daemon was not running on this Mac when checked). |
| Backup cron installed by the production deploy | `deploy/backup-db.sh` (also tars `private_uploads`) and `deploy/restore-test.sh` | Keep ours; the template's `backup_cron.sh` does not tar the caregiver documents volume. |

## 3. Decisions the owner must make

| ID | Question | Recommendation |
|---|---|---|
| **DEP-1** | Adopt the two-branch model? PRs would target `develop`; a release is `git push oakly origin/develop:main`. `main` then needs linear history (our squash merges already give that). | **Yes.** It is the only way staging gets to test exactly what production later runs. Cost: every PR targets `develop` from now on, and "merge #N" means merge into `develop`. |
| **DEP-2** | Where do secrets live? (a) the template's way: encrypted `.env.*.enc` in git, key in GitHub; or (b) on the server only, as our kit does. | **(b) for now**, because the repo is **public** and a ciphertext of every production secret would sit in its history forever. The action needs a small change: skip the decrypt-and-ship step and use the server's file. If the owner makes the repo private, (a) becomes reasonable. Either way the leaked keys must be rotated first. |
| **DEP-3** | Is the public repo intended? A health-data backend in a public repo exposes the code, the migrations and the CI config. | Probably not intended. Making it private costs GitHub Actions minutes (the old account's Actions billing was blocked), so check the plan first. |
| **DEP-4** | One VPS or two? | Staging on one VPS now (nothing else is possible without a server). Production on its own server later, chosen with the latency measurement in `docs/deploy/staging.md`. |
| **DEP-5** | Does the first production release wait for the clinician sign-off and the Meta/Paystack/Dojah live passes? | Yes. The pipeline can exist long before the first `main` push. |

## 4. Work packages, in order

| # | Package | Who | Done when |
|---|---|---|---|
| WP1 | Create `develop` from `main` and push it. Set `develop` as the default branch only after DEP-1 is confirmed. | lead / session | `develop` exists at the same commit as `main` |
| WP2 | Install the template into the backend on a branch `ci/adopt-devops-template` with the flags in section 2. Then **merge by hand**, not `--force`: keep our CI steps and FE-guide checks, take the template's job structure, replace `deploy-staging.yml` (draft) with `cd-staging.yml` / `cd-production.yml`, add `actions/deploy-stack`, `dependabot.yml`, and the docs under `docs/deployment/`. | build session | `actionlint` and `shellcheck` clean; a PR that touches only docs skips tests |
| WP3 | One compose file (`docker-compose.prod.yml`: db, redis, api, worker, `API_IMAGE`), nginx and runbook updated to match, `deploy/deploy.sh` and `compose.sh` retired or reduced to the manual rollback. Add the **pre-migration `pg_dump` step** to the action. | build session | `docker compose config` parses; a scratch stack boots and migrates |
| WP4 | Shard the test suite (`pytest-split` or matrix) so CI is well under 45 minutes; cache the virtualenv. The 2026-11-08 flake (a clock read once at import) shows the cost of long runs crossing midnight. | build session | the same tests pass in N shards; wall time measured |
| WP5 | Staging E2E: wrap `scripts/staging_smoke.py` as the `staging-e2e` job. | build session | job passes against a scratch stack |
| WP6 | GitHub setup, **owner clicks** (click-by-click steps go in `docs/deployment/GITHUB_ACTIONS_SETUP.md`): Environments `staging` and `production` with deployment branches `develop` and `main`, required reviewer on production, secrets `VPS_HOST`, `VPS_USERNAME`, `VPS_SSH_KEY`, `DEPLOY_PATH`, `ENV_ENCRYPTION_KEY` (or the DEP-2(b) variant), variables `APP_URL`, `VPS_PORT`; branch rules: `main` linear history, `develop` PR required + `CI gate`. | owner | each box ticked in the guide |
| WP7 | **First live run** once the VPS exists: runbook sections 1-9, then merge to `develop`, watch staging build, deploy, E2E, verified tag; then the first fast-forward to `main` only per DEP-5. Fix what it finds in the template **and** in the runbook. | lead + owner | staging deploy green; auto-rollback and manual rollback each exercised once |

## 5. Not covered here

- **The monorepo** (`apps/admin` console, the two Expo apps, `packages/api-client`) has its own CI (about 8 minutes) and no deploy. The admin console host (Vercel or the same VPS) is an open owner question from the staging kit. Mobile builds stay manual through EAS; the apps get the server address in `eas.json` only after staging is up.
- **Schema rollback.** Both our kit and the template roll back **code only**. Keep every migration backward-compatible for one release; the pre-migration dump is the schema escape hatch.
- **Trust boundary.** The `staging-verified-*` proof is a plain registry tag; anyone who can push workflows can mint one. Fine while the team is small; attestations are the upgrade path.

## 6. Verification so far

Read-only. Compared the template (README, SOP, `ci.yml`, both CD workflows, `deploy-stack`, `install.sh`) with the backend's `main` (`a4d6b79`): Dockerfile, both compose files, `deploy/`, `boot/docker-run.sh`, `ci.yml`, `deploy-staging.yml`, the staging runbook and SOP; repo visibility and branch protection read through the GitHub API (`public`, none protected). Nothing was run.
