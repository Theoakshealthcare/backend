# SOP: staging deploy kit (everything ready for the first deploy)

| | |
|---|---|
| **What shipped** | A complete first-deploy kit for one Ubuntu VPS, **prepared but not run on any server**: a runbook, a standalone staging compose file, the nginx server blocks, the env file template naming every backend setting, deploy / backup / restore-test scripts, a smoke script, a one-time pricing helper, and a **disabled** CI deploy workflow draft. |
| **Branch / commits** | `chore/staging-deploy-kit`, cut from `oakly/main` at `1e59f8c` (#49). Not pushed; the lead pushes and opens the PR. |
| **Runbook** | [`docs/deploy/staging.md`](../deploy/staging.md): the owner's to-do list at the top, then sections 1-17 (sizing, DNS, server prep, Docker, code and secrets, nginx and Let's Encrypt, first deploy, backups, first-run steps, "how to know staging works", mobile rebuild and push test, rollback, voice notes, CI, operate, verified vs not, follow-ups). |
| **No migration, no API change** | Nothing the apps call changed, so there is no FE integration guide for this work (no new endpoint, no new field). One image change: `Dockerfile` creates `/app/uploaded_notes` so a volume can be mounted there. |
| **Not verified** | **Everything that needs a real server**: the `api` and `worker` services from the staging compose file, real certificates and DNS, `TRUSTED_PROXIES` against the real bridge address, SMTP delivery, Paystack and Dojah callbacks, Expo/Firebase push, the CI workflow, the proposed `eas.json` change and the EAS rebuilds. See the runbook's section 16. |

## Why
The owner has no VPS yet, and the VPS is the single blocker for real-phone testing, push, real email, live Paystack/Dojah and the final app rebuild. The decision is to keep building locally and deploy once the server exists. This work makes that deploy a short checklist instead of an investigation: every decision (sizing, DNS, TLS, volumes, backups, rollback, first-run data, webhooks, mobile env) is made and written down, and the parts that can be proven offline were proven.

## How (decisions)
| Area | Decision | Rejected / why |
|---|---|---|
| Compose | A **standalone** `docker-compose.staging.yml` (db, redis, api, worker; named project `oakly-staging`) driven by `deploy/compose.sh`. No host port for Postgres or Redis; only `127.0.0.1:8000` for nginx. Restart policies, healthchecks (API `/health`, worker `arq --check`, redis ping, `pg_isready`), log rotation, Redis `noeviction` and 256 MB, a fixed subnet `172.29.0.0/24`. | An override of `docker-compose.yml`: the dev file bind-mounts `./app` and publishes db/redis ports, and `ports:`/`volumes:` merge, so an override leaves the dev holes open. The existing `docker-compose.prod.yml` assumes a managed database; staging runs Postgres in compose. |
| Proxy trust | The compose network has a fixed subnet and the env file's `TRUSTED_PROXIES` names it; nginx **sets** `X-Forwarded-For` to `$remote_addr`. The app takes the right-most untrusted hop (`app/core/client_ip.py`). | Appending the client's own header (spoofable); trusting nobody (every phone behind a carrier NAT, and the admin lockouts, would share the proxy's IP). |
| Environment name | `ENVIRONMENT=staging`: boots before Dojah and the KYC widget URL exist. `production` (the pre-launch rehearsal) turns on the boot-time refusals for missing Paystack/Dojah/KYC values. | `production` from day one: blocks the first deploy on third-party accounts. |
| Image | **Build on the server for the first deploy** (no registry to set up); a CI path builds to GHCR tagged by git SHA. Deploy and rollback are "set `OAKLY_API_IMAGE`" (`/etc/oakly/image.env`, previous kept). | Floating `latest` tags: no rollback target. |
| Migrations | `alembic upgrade head` is an explicit one-off step **before** the new API starts; the container boot script also runs it (idempotent). A **rollback is image-only** and skips it (an older image cannot run a newer database's migrations). | Relying only on the boot-time migration: a failure would surface as a crash-looping API. |
| Backups | `pg_dump` custom format nightly from cron, 14 days, plus a tar of the `private_uploads` volume (caregiver documents are not in Postgres); `restore-test.sh` restores into a scratch database and checks it; `deploy.sh` backs up before every migration. | Backups with no restore test. |
| Env file | `deploy/staging.env.example` names **all 140** `Settings` keys, grouped, active or commented out; a pytest keeps it in step. Secrets are filled with `sed` from `openssl rand` so no value is ever printed. `VITALS_ENCRYPTION_KEY` is generated with `openssl rand -base64 32 \| tr '+/' '-_'` (checked to be a valid Fernet key). | A hand-trimmed list (it drifts; an unknown key stops boot). |
| Findings that changed the plan | (1) **SMTP is implicit TLS only** (`smtplib.SMTP_SSL`, `SMTP_TLS` unread): the provider must offer port 465. (2) **`GEMINI_MODEL` is not a setting** on `main`: setting it stops boot. (3) `PAYSTACK_WEBHOOK_SECRET`, `FCM_SERVER_KEY`, `APNS_*` are declared but unused: Paystack signs with the secret key, and push goes through Expo (the Firebase Admin key is uploaded to Expo with `eas credentials`, not to the server). (4) `/booking/preview` is unauthenticated and answers 503 `booking_settings_not_configured` without a pricing row: the smoke script uses it as the pricing check. (5) A fresh install needs MFA enrolment through the API before any admin call, so the pricing helper enrols. | |
| Pricing | **Never auto-seeded.** `scripts/staging_set_pricing.py` requires `--base-fee`, `--platform-fee-percent` and `--reason` (no defaults), shows the numbers, asks yes/no, and does nothing if a row exists. | A default price list in a migration or seed. |
| Workflow | `deploy-staging.yml`: `workflow_dispatch` only, plus the job needs the repo variable `STAGING_DEPLOY_ENABLED=true` and the GitHub Environment `staging` (reviewer gate). SSH key and GHCR token only via secrets; the token goes over stdin. | Deploy on push to `staging`: no human decision, and no server yet. |

## What's involved
| Path | What |
|---|---|
| `docs/deploy/staging.md` | The runbook |
| `docker-compose.staging.yml` | db, redis, api, worker, volumes, subnet |
| `deploy/compose.sh`, `deploy/deploy.sh`, `deploy/backup-db.sh`, `deploy/restore-test.sh` | Wrapper, deploy and rollback, backup, restore test (shellcheck clean) |
| `deploy/nginx/oakly-api.bootstrap.conf`, `deploy/nginx/oakly-api.conf` | HTTP-only challenge block, then the TLS server block |
| `deploy/staging.env.example` | Every setting, grouped, placeholders only |
| `scripts/staging_smoke.py` | Health, webhooks, pricing, email code, login, Paystack link, push token, optional Expo receipt |
| `scripts/staging_set_pricing.py` | First-run pricing row via an admin with `pricing.write` |
| `.github/workflows/deploy-staging.yml` | Draft deploy workflow (disabled) |
| `Dockerfile` | One line: also `mkdir /app/uploaded_notes` (prescription note files get a volume) |
| `app/tests/test_staging_env_example.py`, `app/tests/test_staging_smoke_script.py` | Drift guard for the env file; both scripts against fake servers |
| `docs/checklist/MASTER_CHECKLIST.md` | New section 16 |

## Verification
| Check | Result |
|---|---|
| `docker compose -f docker-compose.staging.yml config` | Parses, standalone and also combined with `docker-compose.yml` (do not combine); a missing password is refused with a clear message; no host port for db/redis |
| Live, throwaway project (removed afterwards) | `db` service healthy; `redis` service refuses a wrong password and uses `noeviction`; backup produced a dump and an uploads archive; `restore-test.sh` passed; the restore-over-live sequence recovered a deliberately broken database |
| `arq --check` | Against a throwaway Redis and the real worker: exit 1 before start, 0 while running |
| nginx | `nginx -t` passes on 1.24 (Ubuntu 24.04) and current stable, both server blocks, throwaway certificates. `http2 on;` was replaced by `listen ... http2` because 1.24 does not know the directive |
| `deploy.sh` | `shellcheck` clean; with a stub in place of compose: step order correct, previous image kept, rollback skips alembic, a failed pull falls back to a local image or stops |
| Tests | `app/tests/test_staging_env_example.py` and `test_staging_smoke_script.py`: 16 passed (30 with the existing `test_settings_env_file.py`) |
| Lint | `ruff check --select E9,F63,F7,F82 .` clean; `actionlint` clean on the new and the existing workflow; `shellcheck` clean; `hadolint` shows only warnings that existed before |
| **Not verified** | A server, a real domain/certificate, real mail, Paystack, Dojah, Expo, the workflow, the EAS builds, the `api`/`worker` services from this compose file (no image was built; a full stack was deliberately not started) |

## Operate / roll back
Everything is in the runbook. To drop the kit: revert the branch (no migration, no runtime code change; the `Dockerfile` line only adds an empty directory).

## Follow-ups
1. **Run the runbook once on a real server**, fix what it finds, then update section 16 of the runbook and the checklist.
2. Admin console on staging (without it, caregivers are approved by API calls only).
3. Off-server backups and a failure alert on the backup cron.
4. Trivy and Semgrep steps in the deploy workflow; decide whether third-party actions must be pinned by SHA.
5. Pin `requires-python` below 3.14 (or fix the worker start) so a fresh environment cannot pick the interpreter `arq` breaks on.
6. Remove the unused settings (`PAYSTACK_WEBHOOK_SECRET`, `FCM_SERVER_KEY`, `APNS_*`, `SMTP_TLS`) in a separate cleanup, or make the mailer honour `SMTP_TLS` (STARTTLS on 587).
7. Apply the proposed `eas.json` diff in the monorepo once the domain exists.
