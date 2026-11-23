# SOP: staging API runs 3 gunicorn workers, with a configurable recycle limit

| | |
|---|---|
| **What shipped** | Staging's API now runs `GUNICORN_WORKERS=3` (was 1) and `boot/docker-run.sh` reads a new `GUNICORN_MAX_REQUESTS` (default 1000, same as before); staging sets it to 5000. PR: see the branch `fix/staging-api-workers`. |
| **No migration, no API change** | No endpoint or field changed, so no FE guide to update. The one image change is `boot/docker-run.sh`. |
| **Not verified** | Live effect on staging (needs the merge and the deploy). The cause of the owner's 120-300 s waits is NOT proven by this change (see Why). |

## Why
On 2026-11-23 the owner tested the new Android APKs against staging and saw waits of 60 to 300 seconds on sign-up (code screen, setup step 1). Read-only checks on the VPS found:

| Finding | Evidence |
|---|---|
| The server is not starved | load 2.4 on 4 vCPU, about 85% CPU idle, 4.8 GB free, no swap, all containers healthy |
| The staging API had **one** worker | `GUNICORN_WORKERS=1` in the container env; one worker process under gunicorn |
| That worker restarted about every 80 s | 11 "Maximum request limit ... exceeded" lines between 13:11 and 13:25 UTC; each reboot of the worker takes about 9 s (started to "Application startup complete"); with one worker that is a full outage |
| Latency from outside is uneven | `/health` and a DB-touching endpoint answer in about 0.5 s, with outliers of 3 s and 12 s that fit the restart gaps |
| nginx gives up at 120 s | `proxy_read_timeout 120s` on both server blocks (owner's paste of `/etc/nginx/sites-available/oakly`), the same as the "about 120 seconds, then back to the splash" the owner saw |

About 1,000 requests per 80 s (roughly 13 per second) arrived during the owner's test window. **Who sent them is not known yet**: the API writes no access log and `/var/log/nginx/access.log` needs `sudo`.

## How
- One worker means a restart is an outage; three workers mean the other two keep answering while one reboots. Staging has RAM for it (an API process is about 150 MB; the stack is expected to stay under about 2.4 GB on the shared 8 GB host).
- Recycling every ~1,000 requests exists to cap memory growth. Staging now recycles at 5,000 (jitter 100 unchanged). The default stays 1000, so production is unchanged until it sets the variable.
- **Rejected:** removing `--max-requests` (it protects against slow leaks); raising `--timeout` or nginx's 120 s (it hides a stall, it does not fix it); more than 3 workers (the VPS also hosts the Timpbills stacks).
- The staging value was changed in `.env.staging.enc` by decrypting in memory, editing two lines and re-encrypting with the same key and parameters (round trip checked, only the names `GUNICORN_WORKERS` and `GUNICORN_MAX_REQUESTS` changed; no values printed). `deploy/env.example` now shows `3` and `5000`.

## What's involved
| Area | Path |
|---|---|
| Boot script | `boot/docker-run.sh` (`--max-requests "${GUNICORN_MAX_REQUESTS:-1000}"`) |
| Staging env (encrypted) | `.env.staging.enc` |
| Example and its test | `deploy/env.example`, `app/tests/test_staging_env_example.py` (`GUNICORN_MAX_REQUESTS` added to the compose-only keys) |
| Docs | `docs/deployment/DEPLOYMENT_GUIDE.md` (memory table), `docs/sop/2026-11-21-deployment-adoption.md` (GUNICORN row) |

## Verification
- `bash -n boot/docker-run.sh`, ruff (CI's selection), and `pytest app/tests/test_staging_env_example.py` (16 passed) locally; CI runs the rest.
- After the deploy: `docker exec oakly-staging-api-1 sh -c 'echo $GUNICORN_WORKERS $GUNICORN_MAX_REQUESTS'` prints `3 5000`, and `docker top oakly-staging-api-1` shows 1 master and 3 workers.

## Operate / roll back
- Revert the PR (or set `GUNICORN_WORKERS` back in `.env.staging.enc` with `scripts/env.sh`) and deploy again. Staging's CD runs on every push to `develop`.

## Follow-ups
- Find what sent about 13 requests per second on 2026-11-23 (nginx log, 14:10-14:26 WAT) and why some requests waited the full 120 s.
- Add request timing to the nginx log (`$request_time`, `$upstream_response_time`) and an access log line to the API, so the next stall can be read instead of guessed (needs the owner's `sudo` on the VPS for nginx).
- Mobile apps: loading states for Resend and code submit, and a client request timeout (separate PR).
