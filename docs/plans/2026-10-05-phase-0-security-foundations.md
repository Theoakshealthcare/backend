# Plan: Phase 0 (security, secrets and foundations)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Goal** | Make the backend safe, and able to complete a booking end to end, before the WhatsApp channel and the admin console are built |
| **Inputs** | Review of 2026-09-24/30; [WhatsApp PRD §6 prerequisites](../prd/PRD-whatsapp-patient-channel.md); [Admin PRD](../prd/PRD-admin-console.md); [master checklist](../checklist/MASTER_CHECKLIST.md) §1–2 |
| **Deployment target** | Docker containers. Render is retired and `render.yaml` has been deleted. |

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

### WP-E: Foundations so a booking can complete (branches from `main`, which now includes WP-A–D)

**Shared rules for all of WP-E**

- **Migrations.** Each sub-package adds **one Alembic migration**, chained to the current head (`6fb30639d5f4`).
  - Sub-packages running in parallel will create sibling heads. The lead re-parents them (`down_revision`) at merge time, so `alembic heads` must show exactly one head on `main`.
  - Verify each migration against a database at the *current* head, as follows:
    1. On `main`, start a throwaway Postgres.
    2. Run `python -m scripts.init_local_db` (with `ENVIRONMENT=development`).
    3. Switch to your branch and run `alembic upgrade head`, then `alembic downgrade -1`, then `alembic upgrade head`.

    Record the output in the SOP. Data-converting migrations must be tested with **seeded rows**.
  - The test harness still builds the schema from the models (`create_all`), so models and migration must agree. Add a test that compares them: run `alembic upgrade head` on a DB built at the old head, then `alembic check` or an equivalent, if feasible.
- **Money.** All new money columns are `BIGINT` kobo. Convert only with `app/core/money.py` (`to_kobo`, and a new `kobo_to_naira` for display).
- **API contract.** Keep the existing JSON field names and **naira** values in responses, so the mobile apps keep working. Internal storage changes; the wire format doesn't. New fields may be added, but not renamed or removed.
- **Admin auth.** Use the existing `get_current_admin` for now; full roles and permissions come with the Admin console. Every admin write goes through `AuditService.record(...)` (E5 creates it).

#### E2: Money in kobo (branch `foundations/kobo`)
- **Migrate these columns to BIGINT kobo**, using `ROUND(x * 100)`, and rename each to `*_kobo`:
  - `bookings.total_amount`, `service_fee`, `platform_fee`
  - `booking_settings.base_fee`
  - `patient_wallets.balance`, `total_deposited`, `total_spent`, `total_refunded`
  - `patient_transactions.amount`
  - `patient_refund_requests.amount`
  - `prescriptions.total_cost`
  - `prescription_settings.base_fee`
- **Writing the migration.** It must be reversible (divide by 100 on downgrade), wrapped in one transaction, and tested on seeded rows that include fractional amounts.
- **Code changes.**
  - Model attributes use the `*_kobo` names.
  - Pricing (`calculate_booking_preview`, `create_booking`, prescriptions) computes in Decimal and stores kobo through `to_kobo`.
  - Settlement, refund and withdrawal code compares kobo integers directly. No floats anywhere in money paths.
- **Responses** still return naira, the same fields, with 2 decimal places.
- **Tests:**
  - a fractional price end to end, from preview through create, Paystack init and settlement, all agreeing;
  - wallet deposit, then payment, then refund, with balances in kobo;
  - the migration upgrade and downgrade on seeded data.
- ⚠️ **Money path: an Opus review is required.**

#### E5: Caregiver approval API (branch `foundations/caregiver-approval`)
- **New `caregiver_skills` table:**
  - Columns: `caregiver_id`, `service_type` (one of nurse, doctor, physiotherapist, caregiver, nutritionist), `sub_specialty` (nullable), `created_by_admin_id`, `created_at`.
  - Unique on (`caregiver_id`, `service_type`, `sub_specialty`).
  - E4 uses this table for matching.
- **Caregiver fields:**
  - Add `approved_by`, `approved_at`, `suspended_at`, `suspension_reason`, `rejection_reason`, `changes_requested`. `changes_requested` is JSON: a list of item keys plus a note.
  - Add `background_check_evidence` (JSON: notes, an optional document id, a provider reference) and `background_check_decided_by` / `_at`.
  - New allowed values: `background_check_status` gains `cleared` and `failed`; `profile_status` gains `suspended` and `changes_requested`.
- **Document fields:** `caregiver_documents` gains `reviewed_by`, `rejection_reason` and `expires_on`.
- **New `admin_audit_log` table** (append-only): `admin_user_id`, `action`, `entity_type`, `entity_id`, `before` (JSON), `after` (JSON), `reason`, `ip`, `user_agent`, `created_at`. Plus `AuditService.record()`.
- **Admin endpoints** under `/api/v1/admin/caregivers` (all `get_current_admin`, all audited):
  - `GET /` lists applications. Filter by `profile_status`, default `under_review`; paginated; oldest first.
  - `GET /{id}` returns the full detail: profile, consents, documents (metadata plus the existing authorized download URL), identity results, background check, skills, and the audit history for that caregiver.
  - `POST /documents/{doc_id}/decision` takes `{decision: verified|rejected, reason?, expires_on?}`.
  - `POST /{id}/identity-decision` takes `{decision: verified|failed, notes}`.
  - `POST /{id}/background-check-decision` takes `{decision: cleared|failed, notes, provider_reference?, document_id?}`. **Mandatory before approval.**
  - `PUT /{id}/skills` sets the full skill list.
  - `POST /{id}/approve` is only allowed when all of these hold, otherwise 409 with a list of what's missing:
    - at least one `license` document is verified, and none is pending;
    - identity is verified;
    - the background check is cleared;
    - at least one skill is set;
    - consents are accepted.

    On success it sets `profile_status=approved`, `approved_by` and `approved_at`.
  - `POST /{id}/reject` takes `{reason}`.
  - `POST /{id}/request-changes` takes `{items[], note}`.
  - `POST /{id}/suspend` takes `{reason}`. Suspended caregivers are excluded from matching; future accepted bookings are flagged for ops.
  - `POST /{id}/reinstate`.
- **Caregiver side:**
  - `GET /caregiver/verification-status` now includes `rejection_reason`, `changes_requested`, and the background check status.
  - Approve, reject, request-changes and suspend each email the caregiver. Use the existing email service, mocked in tests.
- **Form options:** replace the US `states` list with the 36 Nigerian states plus the FCT (AD-FR-56).
- **Tests:**
  - every endpoint returns 401 without a token and 403 for a non-admin;
  - every approve precondition (each missing item gives 409 naming it);
  - an audit row is written for each action;
  - the caregiver cannot set any of these fields themselves;
  - suspended caregivers are excluded from matching (the current `find_nearby_caregivers` path).
- ⚠️ **Permission path: an Opus review is required.**

#### E3 + E4: Jobs and matching (branch `foundations/dispatch`, starts after E5 merges because it needs `caregiver_skills`)
- **E3, infrastructure:**
  - Add Redis 7 and an **ARQ** worker (`app/worker.py`) as docker-compose services, dev and prod.
  - Add a `REDIS_URL` setting.
  - The ARQ cron runs `expire_unpaid_bookings` every minute and `dispatch_tick` every minute.
  - Job functions are plain functions taking a DB session, so tests call them directly without Redis. Add one smoke test that the worker settings import cleanly.
- **Expire unpaid bookings:**
  - New booking field `payment_expires_at`, set to now + `PAYMENT_LINK_TTL_MINUTES` (default 30) at creation.
  - The job cancels bookings that are `pending` with payment `pending` and past expiry: `cancel_reason='unpaid_timeout'`, row lock, idempotent.
  - Replace the old 7-day `auto_cancel_unpaid_bookings`.
- **E4, matching:**
  - Candidates must be: approved, not suspended, holding a `caregiver_skills` row for the booking's `service_type`, with `caregiver_locations.is_available` true and `updated_at` within `LOCATION_FRESHNESS_MINUTES` (15), and within the radius (20 km).
  - Keep the existing scoring for ordering.
  - The free-text `specialization ILIKE` match goes away.
- **Dispatch waves:**
  - New table `booking_dispatch_waves` (`booking_id`, `wave_no`, `notified_caregiver_ids`, `sent_at`, `expires_at`).
  - Booking gains a `dispatch_state` field (`dispatching` | `matched` | `unmatched`). It's separate from `status`, so client enums don't break.
  - Settlement starts wave 1 (top `DISPATCH_WAVE_SIZE` = 8).
  - `dispatch_tick` starts the next wave of not-yet-notified candidates when a wave expires (`DISPATCH_WAVE_MINUTES` = 10) without an accept.
  - After `DISPATCH_ESCALATE_MINUTES` (30), or when no candidates are left, set `dispatch_state=unmatched` and log it, for the admin unmatched queue.
  - Accept sets `matched`.
  - Everything must be idempotent and safe if two workers run.
- **Tests:**
  - expiry (including not expiring paid bookings, and running twice);
  - candidate filtering (each filter);
  - wave progression;
  - escalation;
  - a concurrent tick does nothing twice;
  - accept after escalation still works.

#### E1: Migration baseline ✅ (done 2026-10-05; unblocked by the owner confirming there is no production data)
- Squash the history into a baseline that matches production exactly.
- Stamp production at the baseline.
- Remove `init_local_db` in favour of `alembic upgrade head` on empty DBs.
- Make the test harness build the schema through migrations.

## Order and parallelism
1. WP-A (done) is the base.
2. **WP-C and WP-D run in parallel** in separate worktrees. They touch different files. WP-C owns the caregiver actions in `services/booking.py`; WP-D owns `payment.py`, `patient_wallet.py`, the withdraw route, and the verify-payment route.
3. WP-B runs alongside them, done by the lead.
4. Merge order: A → B → C → D, rebasing as needed.
5. WP-E: **E2 and E5 run in parallel** (E2 owns the money models, services and pricing; E5 owns caregiver, document and admin code). **E3+E4** follow once E5 merges. E1 waits on the owner's schema dump.

## Owner actions (outside the code)
- Rotate each secret using the WP-B runbook once WP-B is merged and deployed.
- Provide a read-only `pg_dump --schema-only` of production for E1.
- Meta business verification (in progress).
