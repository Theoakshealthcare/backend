# Plan: Phase 0 (security, secrets and foundations)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Goal** | Make the backend safe, and able to complete a booking end to end, before the WhatsApp channel and the admin console are built |
| **Inputs** | Review of 2026-09-24/30; [WhatsApp PRD §6 prerequisites](../prd/PRD-whatsapp-patient-channel.md); [Admin PRD](../prd/PRD-admin-console.md); [master checklist](../checklist/MASTER_CHECKLIST.md) §1–2 |
| **Deployment target** | Docker containers. Render is being retired, so `render.yaml` is ignored. |

## Ground rules for every work package (WP)

- **One WP = one branch = one PR.** Base each branch on the branch named in its WP and keep the PR focused.
- **Test first.** Every fix needs a test that **fails before the change and passes after it**. Tests use the harness in `app/tests/` (throwaway Postgres, factories, network blocked). Never call real Paystack, SMTP, Gemini or Cloudinary; mock at the service boundary.
- **Before pushing,** run the CI steps locally and get them green:
  - `poetry check --lock`
  - `ruff check --select E9,F63,F7,F82 .`
  - `python -m scripts.ci_smoke`
  - `pytest`
- **Commits are clean.** No AI attribution, no `Co-Authored-By` trailers. The author is the repo's configured git user.
- **Don't widen scope.** If you notice something else, list it under follow-ups in the PR. Don't fix it in the same PR.
- **Error contract stays FastAPI's default.** Errors return `{"detail": "..."}` with the right status code: 401 not authenticated, 403 authenticated but not allowed, 404 not found, or hidden from a user who must not learn the record exists, 409 state conflict, 422 validation error.
- **Write an SOP** in `docs/sop/` for each WP, and tick its items in the master checklist.

## Work packages

### WP-A: Test harness ✅ (branch `test/harness`, base `chore/poetry-migration`)
- [x] pytest with a testcontainers Postgres, factories, auth helpers and a network guard
- [x] `OAKLY_ENV_FILE=""` disables `.env` loading in tests
- [x] Removed the legacy tests (they hit the live server and production DB, and contained hardcoded JWTs)
- [x] CI runs pytest

### WP-B: Secrets, code side (branch `security/secrets`, base `test/harness`)
1. **Settings come only from the environment.**
   - Required, with no default and a fail-fast error at startup: `SECRET_KEY`, `DATABASE_URL`, `VITALS_ENCRYPTION_KEY`.
   - Remove the hardcoded Supabase host, user and password from `app/core/config.py`.
   - Remove the default superuser email and password.
   - `security.py` reads the encryption key from `settings`, not `os.environ`.
2. **Remove hardcoded credentials** from `alembic.ini` (alembic reads `DATABASE_URL` from settings), `docker-compose.yml` and `scripts/check_booking_patient.py`.
3. **Delete scripts that leak or mislead:**
   - `print_smtp_env.py`, which prints the SMTP password
   - `create_test_admin.py`, which has hardcoded admin credentials. A secure bootstrap comes with the admin work.
   - `app/db/init_db.py`, which is broken and unused
   - `scripts/check_booking_patient.py`
4. **Encryption key rotation.**
   - `VITALS_ENCRYPTION_KEY` accepts a comma-separated list. The first key encrypts; every key in the list can decrypt (Fernet `MultiFernet`).
   - Add `scripts/reencrypt_health_data.py`, which re-encrypts every encrypted column with the newest key. It's idempotent, works in batches, and has a `--dry-run` mode.
   - Tests cover: decrypt with the old key, re-encrypt, decrypt with the new key only.
5. **Docker for local and production:**
   - `docker-compose.yml`: the `api` service gets all config from `env_file: .env`. There's a local `db` service (postgres:16) with a named volume, the app's healthcheck hits its own `/health` with python (curl isn't in the image), and the hardcoded Render URL is removed.
   - Add `docker-compose.prod.yml` (or a profile): no bind mounts, `restart: unless-stopped`.
6. **`.env.example`** lists **every** setting the app reads, with placeholder values and comments.
7. **Stop tracking secrets:** `git rm --cached .env` and every tracked `__pycache__/*.pyc` file.
8. **Runbook** `docs/sop/…-secret-rotation.md`. The owner performs it. For each secret it says where to rotate it (Supabase, Paystack, Zoho, Gemini, Cloudinary), the order to do it in, how to re-encrypt, and how to verify. It also covers rewriting git history (`git filter-repo`) as a separate, coordinated step.

**Acceptance criteria:**
- The app refuses to start without the required environment variables.
- `grep` finds no credential anywhere in the tracked tree.
- The re-encryption script is tested.
- `docker compose up` boots the API and a local Postgres from `.env.example` values.

### WP-C: Auth and access control (branch `security/access-control`, base `test/harness`)
1. **Token type.** `get_current_user` rejects any token whose `type` isn't `access` (a refresh token must not work as an access token). Inactive users get 401/403 on every protected route (`get_current_active_user` is used consistently).
2. **WebSockets** (`/booking/ws/caregiver/location`, `/booking/ws/patient/tracking`):
   - Authenticate with a JWT access token, sent in the `Sec-WebSocket-Protocol` header or as a `token` query parameter. Close with code 4401 when the token is missing or invalid.
   - The caregiver socket requires a caregiver user. A patient may only track bookings they own, and only while the booking is `accepted` or `in_progress`.
   - Remove the hardcoded demo users.
3. **Remove public diagnostics:** `GET /booking/ws/stats` (move it behind admin) and `GET /auth/test-cloudinary` (delete it).
4. **Prescriptions.** Every route requires authentication.
   - A patient can only see and create their own prescriptions.
   - `PATCH /{id}`, `PATCH /{id}/status` and `POST /{id}/upload_note` are **admin-only** for now (the ops tool comes in the Admin console).
   - `GET /patient/{patient_id}` only works for that patient or an admin.
5. **Booking reads.** `GET /booking/booking/{id}` and live-tracking: only the booking's patient, its assigned caregiver, or an admin. Any other `user_type` is refused (today it falls through to "allowed").
6. **Caregiver booking actions** (`services/booking.py:337-394`):
   - `accept`: only a caregiver who was **notified** for this booking, only while it's `pending` **and paid**, and only once. Concurrent accepts must result in exactly one winner (row lock, or a conditional update).
   - `start`, `complete`, `cancel`: only the **assigned** caregiver, and only from valid source states.
   - Anything else returns 403 or 409.
7. **Private documents.**
   - Caregiver uploads go to a private directory **outside** the `/uploads` static mount (a Docker volume, `PRIVATE_UPLOAD_DIR`).
   - Add `GET /caregiver/documents/{id}/file`, which only the owning caregiver or an admin can access, streamed with the right content type.
   - The `/uploads` static mount keeps only public profile images.
   - Add a one-off script that moves existing document files and rewrites `file_path`.

**Acceptance criteria:** each numbered item has tests for: no token, the wrong user, the right user, and a refresh token where applicable.

### WP-D: Money integrity (branch `security/money`, base `test/harness`)
1. **Withdrawals** (`endpoints/caregiver.py:392-432`):
   - `amount` must be `> 0`, and at least a minimum (setting `MIN_WITHDRAWAL_KOBO`).
   - Lock the wallet row (`SELECT … FOR UPDATE`) while checking the balance and deducting.
   - A concurrent double-submit can't overdraw.
2. **Refunds** (`services/patient_wallet.py:408-474`):
   - Approving a refund sends money to **exactly one** destination: Paystack refund to the original payment if it has a Paystack reference, otherwise a wallet credit. Never both.
   - Approving a request twice is impossible (state check plus row lock).
   - Fix the admin check (use `get_current_admin`, not `user_type=="admin"`).
3. **Paystack webhook** (`endpoints/payment.py`):
   - Keep the signature check, and test it.
   - **Idempotency:** a replayed `charge.success` for an already-paid booking or deposit changes nothing and doesn't re-notify caregivers.
   - **Amount check:** the amount paid (kobo) must equal the booking total. On a mismatch, don't mark it paid; log it and store it for review (a minimal `payment_exceptions` record or a logged `payment_failure_reason`).
   - `charge.failed` on an already-paid booking must not cancel it.
4. **`POST /booking/booking/{id}/verify-payment`:** same idempotency and amount rules. It must also trigger caregiver notification exactly once, like the webhook (today it doesn't).
5. **Wallet transactions:** remove the nested `self.db.begin()` (`patient_wallet.py:127,191`), which raises under SQLAlchemy 2. Deposits and payments use one transaction with a row lock on the wallet.

**Acceptance criteria:** tests cover negative, zero, overdraw and concurrent withdrawals; double approval; refund destination; webhook replay, wrong amount, bad signature and failed-after-paid; verify-payment parity.

### WP-E: Foundations so a booking can complete (after C and D merge)
- **E1. Migrations.**
  - Today the Alembic history has two roots and doesn't create the vitals, access or FallGuard tables, so it can't build an empty DB.
  - Replace it with a **baseline** generated from the models and checked against a `pg_dump --schema-only` of production. Stamp production at the baseline.
  - ⚠️ This needs a read-only schema dump of production from the owner.
- **E2. Money in kobo.** Migrate `bookings.total_amount/service_fee/platform_fee`, patient wallet and transaction amounts and prescription `total_cost` from Float/Numeric naira to BIGINT kobo. One pricing helper does all rounding.
- **E3. Jobs.** Add Redis and an ARQ worker plus scheduler as docker-compose services. First jobs:
  - expire unpaid bookings after 30 minutes
  - dispatch timeout and re-dispatch
- **E4. Matching.**
  - Structured caregiver skills (`caregiver_skills`) instead of matching on free-text specialization.
  - Location freshness of at most 15 minutes.
  - Dispatch waves (WA-FR-28/29).
- **E5. Caregiver approval API** (the first slice of the Admin console):
  - Admin endpoints for application list and detail, document decision, identity decision, **background-check decision (mandatory)**, approve/reject and skills.
  - Approval is impossible unless documents and identity are verified **and** the background check is cleared.

## Order and parallelism
1. WP-A (done) is the base.
2. **WP-C and WP-D run in parallel** in separate worktrees. They touch different files. WP-C owns the caregiver actions in `services/booking.py`; WP-D owns `payment.py`, `patient_wallet.py`, the withdraw route, and the verify-payment route.
3. WP-B runs alongside them, done by the lead.
4. Merge order: A → B → C → D, rebasing as needed.
5. WP-E starts after that; E1 waits on the owner's schema dump.

## Owner actions (outside the code)
- Rotate each secret using the WP-B runbook once WP-B is merged and deployed.
- Provide a read-only `pg_dump --schema-only` of production for E1.
- Meta business verification (in progress).
