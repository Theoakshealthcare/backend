# SOP: Secrets out of the code, and the rotation runbook

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `security/secrets` (base `test/harness`) |
| **Work package** | WP-B in the [Phase 0 plan](../plans/2026-10-05-phase-0-security-foundations.md) |
| **Status** | Code side verified locally. **Rotation itself is an owner action** (runbook below). |

## What shipped
- **No credentials in the tracked tree.** The app now reads secrets only from the environment and refuses to start without them.
- **The encryption key can be rotated.** A tested script re-encrypts existing health data with the new key.
- **Docker Compose is env-driven.** It ships with a local Postgres for development and a separate production file.
- **`.env` and 85 `.pyc` files are no longer tracked.**

## Why
Live credentials were committed to git:
- **In `.env`, committed 6 times:**
  - JWT `SECRET_KEY`
  - SMTP password
  - Gemini key
  - Paystack keys and webhook secret
  - Cloudinary keys
  - `VITALS_ENCRYPTION_KEY`
  - `DATABASE_URL`
- **The Supabase DB password**, also hardcoded in `app/core/config.py`, `alembic.ini`, `docker-compose.yml` and `scripts/check_booking_patient.py`.
- **Hardcoded admin and superuser credentials** in `create_test_admin.py` and `config.py`.

Removing them from new commits does **not** make them safe. They stay in history and must be **rotated**.

## How
| Decision | Choice | Rejected |
|---|---|---|
| Required settings | `SECRET_KEY`, `DATABASE_URL` and `VITALS_ENCRYPTION_KEY` have no defaults. Pydantic raises at import and names the missing variable. | A random `SECRET_KEY` per process, which silently logged users out on every restart and differed across workers |
| Encryption key | `VITALS_ENCRYPTION_KEY` is a comma-separated list read through settings into a `MultiFernet`. The first key encrypts; every key decrypts. | Single key: rotating it would make old data unreadable |
| Re-encryption | `scripts/reencrypt_health_data.py`: batched, idempotent, `--dry-run`. A value shaped like a Fernet token that no key can decrypt counts as **unreadable** and is left untouched, never re-encrypted as plaintext. Legacy plaintext vitals are encrypted. | Re-encrypting inside a migration (too slow and too risky for one transaction) |
| Local DB | Compose runs a `db` service (postgres:16). A **development-only** `scripts/init_local_db.py` builds an empty DB from the models and stamps Alembic at head, because the Alembic history can't build an empty DB yet (fixed properly in WP-E1). Production still runs `alembic upgrade head`. | Pointing local dev at the production DB, as the old compose file did |
| Production | `docker-compose.prod.yml` reads its env file from the server (`/etc/oakly/backend.env` by default). Port bound to 127.0.0.1 behind the reverse proxy, `restart: unless-stopped`, private-uploads volume. | Render (being retired) |

## What's involved
| File | Change |
|---|---|
| `app/core/config.py` | Required `SECRET_KEY`, `DATABASE_URL`, `VITALS_ENCRYPTION_KEY`. Removed the hardcoded Supabase host, user and password, the DB URL assembly, and the default superuser. |
| `app/core/security.py` | `MultiFernet` from settings. New `rotate_ciphertext()`. No more `os.environ`. |
| `alembic.ini` | `sqlalchemy.url` is empty; `env.py` sets it from `DATABASE_URL` |
| `docker-compose.yml` | Rewritten: `env_file` (overridable with `OAKLY_ENV_FILE_PATH`), local `db`, real healthcheck against the container's own `/health` (no curl, no Render URL), private-uploads volume |
| `docker-compose.prod.yml` | **New** |
| `Dockerfile` | Creates `/app/private_uploads` and `/app/uploads`; copies `scripts/` into the image |
| `boot/docker-run.sh` | Runs `init_local_db` before migrations, in development only |
| `.env.example` | Every setting the app reads, with placeholders and comments |
| `scripts/reencrypt_health_data.py`, `scripts/init_local_db.py`, `scripts/__init__.py` | **New** |
| Deleted | `print_smtp_env.py`, `create_test_admin.py`, `app/db/init_db.py`, `scripts/check_booking_patient.py`, and the dead `init_db` references in `main.py` |
| Untracked | `.env`, 85 `__pycache__/*.pyc` files (the files stay on disk) |
| `.gitignore` / ruff | `.claude/` (local agent worktrees) is ignored and excluded |

## Verification
- `pytest`: 13 passed, including 8 new tests:
  - Each of the three required settings missing: the app exits and names it.
  - First key encrypts, any key decrypts, rotation works.
  - Re-encryption moves vitals, notes and FallGuard data to the new key, and encrypts legacy plaintext.
  - `--dry-run` writes nothing.
  - With a key missing, the run fails and **does not** double-encrypt. This test was confirmed to fail with the guard removed.
- **Docker:** `docker compose up --build` with a temporary env file built from `.env.example`. The DB was healthy, the dev bootstrap created the schema, `/health` returned 200 and the OpenAPI spec 200. A restart was a no-op. `reencrypt_health_data --dry-run` ran inside the container.
- **Credential scan** of the tracked tree for the Supabase password and project ref, `sk_live_`, long `sk_test_` keys and JWTs: clean. The only matches are truncated `eyJ…` placeholders in an old guide.

## Runbook: rotating the leaked secrets (owner)

Do this **after this PR is merged and the new container deploy is live**, ideally in one maintenance window. Put every new value **only** in the server env file (`/etc/oakly/backend.env`, `chmod 600`) or your secret store. Never put them in git.

| # | Secret | Where to rotate | Notes |
|---|---|---|---|
| 1 | **Supabase DB password** | Supabase → Project Settings → Database → Reset database password | Update `DATABASE_URL`. Old connections drop. |
| 2 | **Paystack secret and public keys** | Paystack Dashboard → Settings → API Keys & Webhooks → generate new keys | Rotate **live** keys too if they were ever used. Update the webhook URL to the new host. |
| 3 | **Paystack webhook secret** | Paystack signs with the secret key; set `PAYSTACK_WEBHOOK_SECRET` to match | Send a test webhook afterwards |
| 4 | **SMTP (Zoho) password** | Zoho Mail → Security → App passwords: revoke the old one, create a new one | Update `SMTP_PASSWORD` |
| 5 | **Gemini API key** | Google AI Studio / Cloud Console → API keys: delete the old key, create a new one | Add API restrictions |
| 6 | **Cloudinary API secret** | Cloudinary Console → Settings → API Keys: generate a new pair, then deactivate the old one | Update `CLOUDINARY_*` |
| 7 | **JWT `SECRET_KEY`** | Generate: `python -c "import secrets; print(secrets.token_urlsafe(64))"` | **Every user is logged out**; the apps must log in again |
| 8 | **`VITALS_ENCRYPTION_KEY`** | See the steps below | Don't skip the re-encryption |

**Encryption key rotation** (no downtime):
1. Generate a key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
2. **Back up the database.**
3. Set `VITALS_ENCRYPTION_KEY=<NEW>,<OLD>` and redeploy.
4. `docker compose -f docker-compose.prod.yml exec api python -m scripts.reencrypt_health_data --dry-run`. Check that `unreadable=0` everywhere.
5. Run it again without `--dry-run`.
6. Set `VITALS_ENCRYPTION_KEY=<NEW>` and redeploy. Spot-check that vitals and FallGuard history still load in the app.
7. Destroy the old key.

**Verify after rotating:**
- The old Supabase password no longer connects.
- An old Paystack test key gets 401.
- Login works.
- A Paystack test webhook is accepted.
- An email is delivered.

**Git history (separate, coordinated step):**
- Rotating makes the leaked values useless. Rewriting history only removes them from the repo.
- If you want that too: run `git filter-repo --path .env --invert-paths` plus a replace-text pass for the DB password, force-push every branch, and have every collaborator re-clone.
- Do it **after** rotation. Open PRs need rebasing afterwards.

## Operate / roll back
- **Local:** `cp .env.example .env`, fill in the values, then `docker compose up --build`.
- **Production:** `docker compose -f docker-compose.prod.yml up -d` with the server env file.
- **Roll back:** revert the merge. The old code needs `.env` present, which it still is locally (only untracked).

## Follow-ups

> **Update 2026-10-05 (E1):** `scripts/init_local_db.py` and the development hook in `boot/docker-run.sh` have been removed. `alembic upgrade head` now builds an empty database. The encryption-key rotation tooling stays for future use; with no stored health data, rotating the key today doesn't need a re-encryption run. See [the migration baseline SOP](2026-10-05-migration-baseline.md).
- WP-E1: an Alembic baseline that replaces `init_local_db` for empty DBs.
- Optional history rewrite (above).
- Patient app: the release keystore and its passwords are committed (`android/app/oakly-upload-key.keystore`, `android/gradle.properties`). Decide whether to request a Play upload-key reset.
