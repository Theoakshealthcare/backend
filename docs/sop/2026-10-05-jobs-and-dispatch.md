# SOP: Background jobs, unpaid expiry, skills-based matching and dispatch waves (WP-E3 + E4)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `foundations/dispatch` (base `main` at `3729a90`, after E5) |
| **Migration** | `9b1d4e7a3c52` (down_revision `2e543a04ee5d`, E5) |
| **Status** | Verified locally; not pushed. Settlement and accept paths changed, so the review should include the money/accept path. |

## What shipped
- An **ARQ worker** (`app/worker.py`) with Redis, as `worker` and `redis` services in both compose files. It runs two cron jobs every minute: `expire_unpaid_bookings` and `dispatch_tick`.
- **Unpaid expiry.** A booking gets `payment_expires_at = now + PAYMENT_LINK_TTL_MINUTES` (30). The job cancels `pending` bookings with payment `pending` past that time (`cancel_reason='unpaid_timeout'`). The old 7-day `auto_cancel_unpaid_bookings` is gone.
- **Matching** now uses `caregiver_skills`, not the free-text `specialization ILIKE`.
- **Dispatch waves.** Paying a booking starts wave 1 (8 caregivers). If nobody accepts within 10 minutes the next 8 not-yet-notified candidates get it. After 30 minutes in total, or when nobody is left, the booking becomes `dispatch_state='unmatched'` and a warning is logged for the admin unmatched queue (built with the admin console).

## Why
The booking flow could not complete on its own: unpaid bookings sat for 7 days, matching trusted a free-text field (and never checked how old a caregiver's location was), and a paid booking got one single burst of notifications with no follow-up (WA-FR-28/29).

## How
| Decision | Choice | Why |
|---|---|---|
| Job bodies | Plain functions `f(db, now=None)` in `app/services/booking_expiry.py` and `app/services/dispatch.py`; `app/worker.py` only schedules them and opens/closes a `SessionLocal` in a thread (`asyncio.to_thread`). | Tests call them directly with an explicit `now`: no Redis, no sleeping. |
| Candidate filter | In `LocationService.find_nearby_caregivers`: `profile_status='approved'`, `suspended_at IS NULL`, `EXISTS caregiver_skills(service_type)`, location `is_active` and `is_available`, `updated_at >= now - LOCATION_FRESHNESS_MINUTES`, within `MATCH_RADIUS_KM`. The existing scoring only orders the result. | One place decides eligibility; preview, dispatch and the surge helper all share it. A caregiver with a NULL `specialization` no longer crashes the scorer. |
| Freshness applies everywhere | `find_nearby_caregivers` always applies the freshness window, not only dispatch. | A stale position is unreliable for the booking-preview count too. |
| Dispatch state | New `bookings.dispatch_state` (`dispatching` / `matched` / `unmatched`, CHECK constraint, NULL for legacy) and `dispatch_started_at`, separate from `status`. | Client enums do not change. |
| Settlement | In the same transaction that marks the booking paid it sets `dispatch_state='dispatching'`; after commit it calls `dispatch.start_dispatch()` (wave 1). A failure there is logged and the tick retries. Only the unpaid-to-paid caller does this, so webhook and verify-payment replays dispatch once. | Committing the state with the payment means a crash between "paid" and "notified" cannot lose the booking. |
| Wave progression | `dispatch_tick` selects `dispatching` + `pending` + unassigned + paid bookings, then for each: lock the row with `FOR UPDATE SKIP LOCKED`, re-check state, then escalate (>= `DISPATCH_ESCALATE_MINUTES` since `dispatch_started_at`), skip (latest wave still open), or send the next wave. Escalation is checked first, so a 30-minute limit is a hard stop even mid-wave. | A second worker skips a booking the first holds, and re-checking under the lock makes the repeat a no-op. |
| Two-worker safety | Row lock + SKIP LOCKED, and `UNIQUE (booking_id, wave_no)` as a backstop. A failure on one booking is logged and does not stop the others. | Tested with two threads on separate sessions. |
| Accept | Unchanged guards; now also sets `dispatch_state='matched'`. Allowed for a caregiver notified in any wave, and after `unmatched`, while the booking is `pending` and paid. | Offers from earlier waves stay valid. A cancelled booking still returns 409. |
| Late payment after expiry | Settlement, under the booking lock: if the booking was cancelled with `cancel_reason='unpaid_timeout'` and `scheduled_date > now + LATE_PAYMENT_REINSTATE_MIN_LEAD_MINUTES` (60), it is **reinstated** in the same transaction (`pending`, reason cleared, `dispatch_state='dispatching'`) and `start_dispatch` runs after commit, exactly like a normal payment. Otherwise it stays cancelled, `refund_required=true` and `refund_required_reason` (`paid_after_expiry`, or `paid_after_cancellation` for any other cancelled booking) are set, and an ERROR is logged. Nothing is refunded automatically; the admin refund queue will read the flag. | The money is real. Expiry and settlement converge on the same end state whichever wins the row lock. |
| Verify-payment response | Normal shape when paid or reinstated. When the booking stays cancelled (also on a repeat call): HTTP 200 `{"status":"success","message":"...","booking_id":...,"payment_status":"paid","booking_status":"cancelled","refund_required":true}`. The webhook still returns 200. | The app must tell the patient a refund is coming, not "booking confirmed". |
| Location freshness | `update_caregiver_location` sets `updated_at` explicitly on every ping (REST and WebSocket both use it). | `onupdate` does not fire when a stationary caregiver re-sends identical values, so they aged out of matching. |
| Zero candidates | At wave 1 with nobody eligible the booking stays `dispatching` and retries every tick; it becomes `unmatched` only once `DISPATCH_ESCALATE_MINUTES` has passed. Running out of candidates after at least one wave still escalates immediately. | A caregiver may come online within the window. |
| Paystack callback | `create_booking` now sends `FRONTEND_URL/bookings/<id>/payment-complete` (it was always None because a local variable shadowed the config). | Bug fix. |
| Legacy rows | Existing unpaid bookings have no `payment_expires_at`; the job uses `created_at + TTL` for them. No data migration. | The migration stays a pure schema change and fully reversible. |

Rejected: Celery (the plan and the stack choose ARQ: async-native, small); setting `dispatch_state` only after commit in a second step (loses a booking if the process dies in between); advisory locks (row locks already serialise on the same row).

## What's involved
- Models: `app/db/models/booking.py` (`payment_expires_at`, `cancel_reason`, `refund_required`, `refund_required_reason`, `dispatch_state`, `dispatch_started_at`, `BookingDispatchWave`).
- Services: `app/services/dispatch.py`, `app/services/booking_expiry.py` (new); `app/services/location.py` (candidate filter); `app/services/booking.py` (`notify_nearby_caregivers` and `auto_cancel_unpaid_bookings` removed, `smart_caregiver_matching(now=)`, expiry set at create, accept sets `matched`); `app/services/booking_payment.py` (settlement starts dispatch).
- Worker: `app/worker.py`; `arq==0.26.3` added to `pyproject.toml`/`poetry.lock` (brings `redis`).
- Config: `app/core/config.py`, `.env.example`; `docker-compose.yml`, `docker-compose.prod.yml` (`redis`, `worker`).
- Migration: `app/alembic/versions/9b1d4e7a3c52_jobs_and_dispatch.py`.
- Tests: `test_booking_expiry.py`, `test_dispatch.py`, `test_dispatch_migration.py`, `test_worker.py`; the four `test_money_*` files that stubbed `notify_nearby_caregivers` now stub `dispatch.start_dispatch`.

### New settings
| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Worker broker. Compose points the worker at `redis://redis:6379/0`. |
| `PAYMENT_LINK_TTL_MINUTES` | 30 | Unpaid bookings are cancelled after this. |
| `LOCATION_FRESHNESS_MINUTES` | 15 | Older caregiver locations are not matched. |
| `LATE_PAYMENT_REINSTATE_MIN_LEAD_MINUTES` | 60 | A late payment reinstates the booking only if the visit is further away than this. |
| `MATCH_RADIUS_KM` | 20 | Matching radius. |
| `DISPATCH_WAVE_SIZE` | 8 | Caregivers notified per wave. |
| `DISPATCH_WAVE_MINUTES` | 10 | Wait before the next wave. |
| `DISPATCH_ESCALATE_MINUTES` | 30 | Total time before `unmatched`. |

## Verification
- Baseline on `main`: 339 passed. After: **399 passed** (`poetry run pytest -q -p no:warnings`), plus `poetry check --lock`, the ruff bug-class lint and `scripts.ci_smoke` as in CI.
- Tests written against the plan list: expiry (expires, keeps paid, only `pending`, rerun is a no-op, legacy rows, late payment after expiry, create sets the expiry); one test per filter (unapproved, suspended status, suspended flag, no skill, wrong skill, stale, unavailable, too far) plus ILIKE-no-longer-matches and the NULL `specialization` crash; wave 1 of 8, waves never overlap, tick idempotent, escalation by time (30 min, no fourth wave) and by exhaustion, stale-between-waves, cancelled and legacy bookings ignored; two concurrent ticks (threads, separate sessions, three bookings) notify each caregiver once; a tick skips a booking another session holds locked; accept from wave 1 after wave 2, accept after `unmatched`, accept sets `matched`; settlement via webhook twice plus verify-payment gives one wave; settle twice gives one wave; a failed wave 1 is recovered by the tick.
- `test_worker.py`: `WorkerSettings` imports and registers both crons at second 0 of every minute; one job runs through a real Redis (testcontainers) with an ARQ burst worker.
- Migration, automated: `test_dispatch_migration.py` upgrades/downgrades only its own revision (never `head`) on a scratch database with seeded rows, checks `compare_metadata` against the models is empty, and asserts exactly one Alembic head.
- Migration, manual (Postgres 16; schema built on `origin/main` (`3729a90`) by `ENVIRONMENT=development python -m scripts.init_local_db`, one booking seeded):

```
alembic heads           9b1d4e7a3c52 (head)
alembic upgrade head    Running upgrade 2e543a04ee5d -> 9b1d4e7a3c52
alembic downgrade -1    Running downgrade 9b1d4e7a3c52 -> 2e543a04ee5d
alembic upgrade head    Running upgrade 2e543a04ee5d -> 9b1d4e7a3c52
alembic current         9b1d4e7a3c52 (head)
seeded booking afterwards: CHAIN001 | PENDING | PENDING | NULLs in the new nullable columns, refund_required false
```

## Operate / roll back
- Run locally: `docker compose up --build` starts `api`, `worker`, `db`, `redis`. Without Docker: `redis-server` and `poetry run arq app.worker.WorkerSettings`.
- Production: `docker compose -f docker-compose.prod.yml --env-file /etc/oakly/backend.env up -d`. The worker waits for the API to be healthy (it runs `alembic upgrade head`). Set the new variables in the server env file only if the defaults are wrong; `REDIS_URL` is forced to the compose service. Redis keeps an append-only file in the `redis_data` volume; nothing in it is the source of truth (jobs are idempotent, state is in Postgres).
- Deploy order: ship the migration and API first (the old code ignores the new columns), then the worker. Without the worker nothing expires and no further waves go out; wave 1 still goes out at payment.
- Logs: `docker compose logs worker`. Look for `dispatch_tick: advanced N booking(s)`, `expire_unpaid_bookings: cancelled N booking(s)`, and the warning `Booking <id> unmatched (...)`.
- Find unmatched bookings: `SELECT id, booking_code, dispatch_started_at FROM bookings WHERE dispatch_state = 'unmatched' AND status = 'PENDING';`
- Roll back: `alembic downgrade -1` drops the waves table and the six new booking columns (including the refund flags) (dispatch history is lost; nothing else). Old code then notifies once at payment again, and the 7-day auto-cancel does not come back (it was removed with no scheduler), so unpaid bookings would simply sit.

## Follow-ups
- The admin "unmatched" queue and the `matching_delayed` message to the booker (WA-FR-29) are not built; only the state and the log line exist.
- WA-FR-29 allows 2 hours before escalation for bookings scheduled more than 24 hours ahead (marked **[DECISION]** in the PRD); a flat 30 minutes is used until that is decided.
- Notifications are still rows in `booking_notifications`; there is no push send yet (`is_sent` stays false), so wave timing is correct but delivery depends on the caregiver app polling.
- A paid booking cancelled while `dispatching` keeps `dispatch_state='dispatching'` (the tick ignores it because `status` is not `pending`); ops/refund handling for cancelled paid bookings is outside this change.
- `BookingService` methods still shadow `settings` with a `BookingSettings` local, which is why the config is imported as `app_settings` there.
- The admin refund queue must consume `bookings.refund_required` (`SELECT ... WHERE refund_required`); nothing refunds automatically.

## Addendum: Redis hardening (automated security scan finding)
ARQ deserialises job payloads it reads from Redis. With the default **pickle** serializer, an unauthenticated Redis that's reachable on the network means remote code execution in the worker. Changes:
- **JSON instead of pickle.** The worker uses JSON (`app/worker.py` `json_serializer` / `json_deserializer`). Anything enqueuing jobs must pass the same serializer.
- **Password required.**
  - Redis runs with `--requirepass`. `REDIS_URL` carries the password, and `REDISCLI_AUTH` covers the healthcheck.
  - Production compose fails to start if `REDIS_PASSWORD` is unset.
  - Dev defaults to `oakly-dev-redis`.
- **Network exposure.**
  - Production doesn't publish Redis to the host.
  - Dev binds Redis, Postgres and the API to `127.0.0.1` only. Before this, they listened on every interface.
- **Verified on a live stack.** Redis was healthy, the worker connected and registered both crons, an unauthenticated `PING` returned `NOAUTH`, and the port was bound to `127.0.0.1`.
- **New setting:** `REDIS_PASSWORD`. Add it to the server env file before deploying.
