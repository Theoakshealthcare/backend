# CI/CD Pipeline — Oakly API

## The idea: build once, test that image, ship that image

The image is built **one time**, on `develop`. That exact image is deployed to staging and tested. Production later redeploys the **same image**, identified by its digest, and never builds anything. What you tested is what you ship.

```
PR → develop                 push to develop                         push to main
     │                            │                                       │
     ▼                            ▼                                       ▼
 ┌────────┐             ┌──────────────────┐                      ┌────────────────┐
 │  CI    │             │ CD (staging)     │                      │ CD (production)│
 │ lint   │             │ 1 build + push   │                      │ 1 find the     │
 │ test   │  merge      │   image (GHCR)   │   fast-forward       │   verified     │
 │ semgrep│ ───────▶    │ 2 deploy staging │   main to develop    │   image        │
 │ image  │             │ 3 smoke test     │ ──────────────────▶  │ 2 deploy it    │
 │ scan   │             │ 4 mark verified  │                      │   (no build)   │
 └────────┘             └──────────────────┘                      └────────────────┘
                                 image  ghcr.io/<owner>/<repo>@sha256:…  (same bytes)
```

## Files

| File | Runs on | Does |
|---|---|---|
| `ci.yml` | PR to `develop`; push to `develop` | Lint and static checks (bug-class ruff, import smoke test, FE-guide `--check`), the test suite in 4 shards, Semgrep. On PRs also builds the image (not pushed) and scans it with Trivy. `CI gate` is the single check to require. |
| `cd-staging.yml` | push to `develop`; manual | Build and push the image, deploy to staging, run the smoke test, then mark the image verified. Off until `STAGING_DEPLOY_ENABLED=true`. |
| `cd-production.yml` | push to `main`; manual (rollback) | Find the verified image for this commit and deploy it. Off until `PRODUCTION_DEPLOY_ENABLED=true`. |
| `registry-cleanup.yml` | weekly; manual (dry run by default) | Deletes old image versions from GHCR, keeping what a rollback needs. |
| `actions/deploy-stack` | called by both CD workflows | The shared deploy: decrypt env, ship, then run `scripts/remote_deploy.sh` on the VPS: pull by digest, back up, migrate, start, health check, roll back on failure. |
| `scripts/ghcr_digest.sh` | called by CD | Looks up an image's digest in GHCR. |

## How production knows an image was tested

After the smoke test passes, `cd-staging.yml` adds two tags to the tested image:

- `staging-verified-<full commit sha>` — "this commit passed staging". Used when `main` is pushed.
- `verified-sha256-<digest>` — "these exact bytes passed staging". Used for manual rollbacks.

`cd-production.yml` looks the tag up in the registry. No tag, no deploy. Only `cd-staging.yml` can add these tags, and only after the smoke test passes.

## Why `main` must be a fast-forward of `develop`

Production finds the image by the commit SHA. A fast-forward keeps the SHA unchanged; a squash or merge commit creates a new SHA that no image was built for. See `BRANCHING.md` for the exact commands and branch settings.

## Keeping images tidy

Two places hold images, and each has a cleanup:

| Where | Cleanup | Keeps |
|---|---|---|
| The VPS | `remote_deploy.sh` runs `docker image prune -af --filter "until=72h"` after each successful deploy | The running image, plus unused images built in the last 72 hours (the filter counts from the image's build time, not when it was pulled). Rollback re-pulls from GHCR if needed. |
| GHCR | `registry-cleanup.yml` (weekly), using `scripts/ghcr_prune.sh` | 20 newest versions, 10 newest staging-verified versions, the tips of `main` and `develop`, anything under 14 days old. |

The limits are variables at the top of `ghcr_prune.sh` (`KEEP_NEWEST`, `KEEP_VERIFIED`, `MIN_AGE_DAYS`). Preview a cleanup first: **Actions → Registry cleanup → Run workflow** (dry run is ticked by default). A manual rollback can only target an image that still exists, so the last 10 verified images are your rollback window.

## Trust boundary: who can release

The "verified" tags are ordinary registry tags. The pipeline's safety rests on **who can run workflows with deploy secrets**, so:

- Set **Deployment branches** on both Environments (`GITHUB_ACTIONS_SETUP.md`, Step 3). This is the control that keeps other branches away from secrets.
- Treat repo write access as release power: anyone who can push workflow changes to `develop` can make an image look verified. Review changes to `.github/` like production code.
- If you need to defend against that, replace the tags with signed attestations (cosign or GitHub artifact attestations) that production checks against the `cd-staging.yml` identity. The template leaves that out to stay simple.

All actions are pinned to full commit SHAs. `.github/dependabot.yml` opens one weekly PR with the bumps; review it like any dependency update.

## CI cost controls

| Control | Effect |
|---|---|
| `main` is not a CI trigger | A commit arriving on `main` was already tested; it is not tested again. |
| Docs-only PRs skip lint, tests and the image scan | Semgrep still runs. A docs-only PR is one whose every changed file is under `docs/` or ends in `.md`. |
| The test suite is split into 4 shards | Wall time drops from about 55 minutes to roughly a quarter plus setup. **Billed minutes do not drop** (each shard repeats setup, so the total is a little higher): on a private repo every code PR costs about one hour of Actions minutes. Check the organisation plan's included minutes. |
| Draft PRs run nothing | Mark ready for review to run CI once. |
| Superseded PR runs are cancelled | Three quick pushes cost one run. |
| `CI gate` job | One check reports even when jobs are skipped, so branch protection stays simple. |

## Tests, and the smoke test against staging

| Stage | What it runs | Real network? |
|---|---|---|
| CI `Tests` (4 shards) | `pytest` over `app/tests`, starting a throwaway Postgres with testcontainers; each shard runs a quarter of the tests (`pytest-split`: `--splits 4 --group N`) | No |
| CD `Live smoke test` | `python3 -m scripts.staging_smoke $APP_URL` against the deployed staging stack | **Yes** |

The smoke script (standard library only, no install) runs without `--email`, so it checks only what needs no person and no mailbox: `/health` through nginx and TLS, the OpenAPI document, both webhooks refusing an unsigned call (Paystack 400, Dojah 401), and the pricing row (`POST /booking/preview` is 200). The email, payment-link and push-token checks need a person and stay in the manual pass (`docs/deploy/staging.md` section 10).

| Situation | Behavior |
|---|---|
| A check fails | The job fails; the image is **not** verified and cannot reach production. |
| Fresh staging, no pricing row yet | The pricing check fails with 503 `booking_settings_not_configured`, so the first image is not verified until the owner publishes the one-time pricing row (`docs/deploy/staging.md` section 9.2). Then re-run the workflow. This is deliberate: a booking flow without prices is not ready for production. |

**Balancing the shards.** Without a durations file `pytest-split` balances by test count. To balance by time: run the suite once with `--store-durations` (CI: add that flag to one run and download the `.test_durations` file from the log or artifact), commit the file at the repo root, and `pytest-split` uses it from then on.

## Deploy mechanics (`deploy-stack` and `scripts/remote_deploy.sh`)

1. The action checks every secret is set, that `DEPLOY_PATH` is `/opt/oakly-staging` or `/opt/oakly-production`, decrypts `.env.<env>.enc` on the runner, and checks the file names this environment's project (`COMPOSE_PROJECT_NAME=oakly-<env>`).
2. It ships `.env`, `docker-compose.prod.yml` and three scripts (`remote_deploy.sh`, `backup_cron.sh`, `restore_test.sh`) to `$DEPLOY_PATH`.
3. `remote_deploy.sh` runs on the VPS: record the running image (by compose labels), log in to GHCR with the run's own short-lived token, pull the image **by digest**, pin `API_IMAGE` in `.env`, start `db` and `redis`, **take a backup** (a failed backup stops the deploy), run `alembic upgrade head` in a one-off container, then `docker compose up -d --wait` so the deploy continues only when `api`, `worker`, `db` and `redis` are all healthy, and finally probe the published port.
4. If any step fails, the previous image is restored for `api` and `worker`, the run fails, and the log names the newest backups. Both staging and production install a daily backup cron (staging 02:15, production 02:00 UTC).

No registry password is stored anywhere: the token is created for each run and expires when it ends. The script was exercised on a scratch stack before adoption: first deploy, redeploy, restore test, and a deliberately broken image that rolled back (`docs/sop/2026-11-21-deployment-adoption.md`).

## Optional: companion frontend E2E

If a frontend repo has its own E2E suite, add a job to `cd-staging.yml` after `staging-deploy` that dispatches the frontend workflow against `$APP_URL` and polls for its result, and add it to the `needs:` of `mark-verified`. It is left out to keep the template simple.
