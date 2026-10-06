# SOP: Backend support for the caregiver app (C3)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `feat/caregiver-mobile-support` (base `main` at `694bd36`) |
| **Plan** | C3 in the caregiver app plan (`docs/plans/2026-10-05-caregiver-app.md` in the caregiver monorepo) |
| **Migration** | `0002_caregiver_mobile` (down_revision `0001_baseline`) |
| **Status** | Verified locally and against a live docker compose stack; not pushed. Needs the Opus review the plan asks for (auth, push, the visit code). |
| **FE guide** | [`docs/fe-integration-guide-caregiver.md`](../fe-integration-guide-caregiver.md), built from [`docs/fe-captures/caregiver/`](../fe-captures/caregiver/) |

## What shipped
| # | Item | Result |
|---|---|---|
| 1 | Push tokens | `push_tokens` table; `POST` and `DELETE /api/v1/devices/push-token`; `PushService` (Expo Push API, `httpx`, timeout, batches of 100, prunes `DeviceNotRegistered`). |
| 2 | Job offers by push | Each dispatch wave pushes "New visit request" to the caregivers it notified, after the wave is saved. Best effort. Payload: booking id, area, time. |
| 3 | Availability | `POST /api/v1/caregiver/availability` `{"is_available": bool}`. Chosen over documenting the location endpoint (decision below). |
| 4 | Visit lifecycle | New status `en_route`, new caregiver action `on_my_way`, 4-digit visit code made at accept, required by `start`, 3 wrong tries lock it for 15 minutes. |
| 5 | Home summary | `GET /api/v1/caregiver/home`. |
| 6 | FE guide and captures | `docs/fe-integration-guide-caregiver.md`, 130+ real captures, `scripts/capture_caregiver_flow.py`. |
| 7 | Migration | One revision on top of the baseline. |

## Why
The caregiver app is a prototype with mocked data. To run the supply side (go available, get an offer, accept, run the visit) the backend needed push delivery, an availability switch, a way to confirm arrival, and one home call. The patient-facing visit code (WA-FR-34/35) was backend-only and unbuilt.

## How: decisions and tradeoffs
| Decision | Choice | Alternative rejected |
|---|---|---|
| Visit code storage | **Encrypted** with the existing Fernet helper (`app/core/security.encrypt`), not hashed. `start` decrypts and compares in constant time (`hmac.compare_digest`). | The plan says "store only its hash", but the same plan has `GET /booking/booking/{id}` return the code to the patient, and a hash cannot be read back. A 4-digit code is also trivially brute-forced from a hash. Encryption keeps it unreadable in a DB dump and still readable by the patient. **The reviewer should confirm this deviation.** |
| Code wrong-attempt counting | Counter and lock timestamp on the booking row, updated under the existing `FOR UPDATE` booking lock. The wrong guess is **committed before raising** (the action handler rolls back on error, which would discard the count). | A Redis counter: a second moving part for something that already has a row lock. A test fires 6 parallel wrong guesses and expects exactly `400, 400, 423, 423, 423, 423`. |
| Lock | 3 wrong -> 15 minutes (`VISIT_CODE_MAX_ATTEMPTS`, `VISIT_CODE_LOCK_MINUTES`). Counter resets to 0 when the lock starts, so the lock expiry gives 3 fresh tries. While locked even the right code gets `423`, and nothing is counted. State is checked before the code, so a non-startable booking is a `409` that never counts. | Counting while locked: a patient could be locked out forever by retries. |
| Status codes for the code | missing or wrong: `400`; locked: `423` with `Retry-After`; malformed (not `^[0-9]{4}$`): `422`; booking has no code: `409`. | One `403`: indistinguishable from the "not approved" 403s the app already maps. |
| Legacy bookings | A booking accepted before this migration has no code; `start` returns `409 This booking has no visit code. Contact support`. Fails closed. | Skipping the check when there is no code: lets a caregiver bypass the control by picking an old booking. No production data exists (see the baseline SOP). |
| Who sees the code | Only the booking's own patient, from `GET /booking/booking/{id}`, only while `accepted` or `en_route`. `enrich_booking_with_details` strips the code columns and sets `visit_code: null`, because the action endpoint returns that dict **without a response model**. | A response-model-only filter: the action response would have leaked the ciphertext. Tested. |
| `on_my_way` | Optional. `start` is allowed from `accepted` or `en_route`. `cancel` is allowed from `accepted`, `en_route`, `in_progress`. `complete` still needs `in_progress`. | Making it mandatory: breaks every client that goes straight from accept to start. |
| `en_route` in the enum | Added to `BookingStatus`; Postgres enum gets `EN_ROUTE`. Live tracking (REST and WebSocket) includes it, which is the whole point of the status. | A separate flag: the plan asks for a status. |
| Push after commit | `dispatch._send_wave` collects what to push; `start_dispatch` and `dispatch_tick` send **after** the commit that saves the wave. `_push_offers` and `PushService.send_to_users` never raise. | Pushing inside the transaction: a failure or a slow Expo would hold row locks or roll back a wave. |
| Push payload | `title` "New visit request"; `data` = `type`, `booking_id`, `area` (city), `scheduled_at`; `body` = service type, city, local time (`Africa/Lagos`). No patient name, condition, address or instructions; a test searches the raw request for each. | Including the booking code or address "for convenience". |
| Push transport | `httpx` with `EXPO_PUSH_TIMEOUT_SECONDS` (5), `EXPO_PUSH_URL` (default Expo), optional `EXPO_ACCESS_TOKEN`. Tests inject `httpx.MockTransport` (`app.services.push._transport`); the suite's network guard blocks anything else. | A fake `PushService` class: it would not test the real request building. |
| Token pruning | Only the `DeviceNotRegistered` ticket error deletes a token. Other errors (rate limit, payload too big) keep it. Receipts are not polled. | Pruning on any error: a transient Expo problem would unregister healthy devices. |
| Token ownership | `token` is unique. Registering a token that belongs to another user **moves it to the caller** (atomic `INSERT ... ON CONFLICT DO UPDATE`). `DELETE` only removes the caller's own row and returns `204` either way. | Rejecting with a conflict: a phone that changes hands would keep sending the previous user's pushes to the new login's device. |
| Availability contract | New endpoint. Approved caregivers only (`403` otherwise). Going on needs an existing location row (`409`); going off with none is a no-op. It writes only `is_available` and **leaves `updated_at` alone** (naming the column in the `UPDATE` stops `onupdate` from refreshing it; a test caught this), so a toggle cannot make a stale position look fresh to matching. | Documenting `POST /booking/caregiver/location` as the contract: it carries a position (a battery cost and permission just to flip a switch) and defaults `is_available` to true. |
| Home "today" | The Africa/Lagos calendar day (`HOME_TIMEZONE`): accepted visits due before the next local midnight (overdue ones stay), plus any visit already `en_route` or `in_progress`. `upcoming` = accepted, due later. Capped at 50. | UTC days: a visit at 00:30 Lagos would show as yesterday's. |
| Home offers | Only for `approved` caregivers; only unanswered `new_booking` notifications on bookings that are `pending`, unassigned and paid. Minimal fields, no patient data. Wallet balance is `wallet_balance_naira` (explicit unit). | Reusing the notifications endpoint: it returns the whole booking (see follow-ups). |
| `httpx` | Moved from the dev group to the main dependencies. | Leaving it dev-only: the production image installs `--only main`, so the lazy import inside payment settlement would have failed in production. **Found by the live run** (it returned 500 from the Paystack webhook). |

## What's involved
- **Migration:** `app/alembic/versions/0002_caregiver_mobile.py`: `push_tokens` (unique `token`, index on `user_id`, `platform` check, FK `ON DELETE CASCADE`); `ALTER TYPE bookingstatus ADD VALUE 'EN_ROUTE'`; `bookings.visit_code_enc`, `visit_code_failed_attempts` (default 0), `visit_code_locked_until`. Postgres cannot drop an enum value, so the downgrade moves `EN_ROUTE` bookings to `ACCEPTED`, then rebuilds the type.
- **Models:** `app/db/models/push_token.py` (new); `app/db/models/booking.py` (`EN_ROUTE`, three columns).
- **Services:** `app/services/push.py` (new); `app/services/caregiver_home.py` (new: home and availability); `app/services/dispatch.py` (push after commit); `app/services/booking.py` (code generation, `on_my_way`, `_check_visit_code`, `BookingVisitCodeError`); `app/services/websocket_manager.py` (`en_route` counts as active).
- **API:** `app/api/v1/endpoints/devices.py` and `caregiver_app.py` (new), registered in `app/api/v1/api.py`; `endpoints/booking.py` (action error mapping, patient-only `visit_code`, tracking allows `en_route`); schemas `devices.py`, `caregiver_app.py`, `booking.py` (`on_my_way`, `visit_code` in the request; `dispatch_state` and `visit_code` in `BookingResponse`).
- **Config:** `app/core/config.py`, `.env.example`: `EXPO_PUSH_URL`, `EXPO_PUSH_TIMEOUT_SECONDS`, `EXPO_ACCESS_TOKEN`, `VISIT_CODE_MAX_ATTEMPTS`, `VISIT_CODE_LOCK_MINUTES`, `HOME_TIMEZONE`. `pyproject.toml` and `poetry.lock`: `httpx` is a main dependency.
- **Tests:** `test_push.py`, `test_dispatch_push.py`, `test_caregiver_availability.py`, `test_visit_lifecycle.py`, `test_caregiver_home.py`, `test_caregiver_mobile_migration.py`; shared `app/tests/fakes.py` (fake Expo); `make_booking(visit_code=...)`; one existing test (`test_access_booking.py`, `start`) now sends the code.
- **Docs and tooling:** the FE guide, `docs/fe-captures/caregiver/`, `scripts/capture_caregiver_flow.py`.

### New settings
| Variable | Default | Meaning |
|---|---|---|
| `EXPO_PUSH_URL` | `https://exp.host/--/api/v2/push/send` | Where pushes go. Point it at a local receiver to capture them. |
| `EXPO_PUSH_TIMEOUT_SECONDS` | 5 | The push runs inline right after payment settles, so keep it short. |
| `EXPO_ACCESS_TOKEN` | unset | Only if "enhanced push security" is on for the Expo project. |
| `VISIT_CODE_MAX_ATTEMPTS` | 3 | Wrong codes before the lock. |
| `VISIT_CODE_LOCK_MINUTES` | 15 | How long it stays locked. |
| `HOME_TIMEZONE` | `Africa/Lagos` | What "today" means on the home screen. |

## Verification
- **Baseline (before any change):** `origin/main` at `694bd36`, full suite green (394 tests).
- **After:** `poetry run pytest -q -p no:warnings`: **510 passed**. Also clean: `poetry check --lock`, `poetry run ruff check --select E9,F63,F7,F82 .`, `scripts.ci_smoke`. `alembic heads` shows one head.
- **Tests were written first** for each item (they failed on missing models/routes), then the code. Covered: 401 and 403 on every new route (refresh token as bearer, patient on a caregiver route, unapproved caregiver); ownership (another caregiver cannot `on_my_way` or `start`, a stranger patient cannot read the booking); every invalid transition returns `409`; code: right, wrong, missing, malformed, three-strikes lock, right-code-while-locked, expiry, fresh tries after expiry, counter reset, rounding of the lock text, **6 concurrent wrong guesses**, state-before-code, legacy booking; the code never appears in the accept response, lists, or to the caregiver and admin (checked on JSON values, not substrings, which flake on 4 digits); push: URL, timeout, optional bearer, batching 250 -> 100/100/50, pruning in and across batches, non-dead errors keep the token, HTTP 500, timeout, connection error, garbled response, an unmocked call hitting the network guard; dispatch push: payload exact and free of patient data (condition, name, address, instructions, phone, email searched for), sent after commit (checked from a second connection), later waves push only the newly notified, replays push nothing, **a failing or exploding push leaves the wave sent exactly once** across a replay and a tick, one booking's failed push does not stop the tick.
- **Migration, automated:** `test_caregiver_mobile_migration.py` touches only `0001_baseline` and `0002_caregiver_mobile` on a scratch database (never `head`): up, down, up; enum labels before and after; `EN_ROUTE` bookings become `ACCEPTED` on downgrade and other statuses survive; existing rows get a zero counter; token unique, platform check and cascade. `test_migrations.py` (models equal migrations, one head, downgrade to base) still passes.
- **Live:** `docker compose -p oakly-c3 up --build` with a temp env file (`OAKLY_ENV_FILE_PATH`), then `scripts/capture_caregiver_flow.py`: 130+ real requests end to end (register, approval by an admin seeded in the DB, availability, location, WebSocket, push tokens, a signed Paystack webhook that starts dispatch, a push request received by a local fake Expo, accept races, the whole visit with the code lock, home, wallet). The live run **found the `httpx` production-dependency bug**. The stack was shut down with `down -v`.

```
alembic upgrade (live stack)   Running upgrade 0001_baseline -> 0002_caregiver_mobile
```

## Operate / roll back
- **Deploy:** ship the migration and API together (`alembic upgrade head` runs on boot). New env vars are all optional. Nothing needs to change on the worker except that it also pushes when it sends a later wave.
- **Push in production:** pushes go to `exp.host`. No Expo credentials are needed unless enhanced push security is enabled.
- **Logs:** `push: batch of N failed (...)` and `push: Expo rejected ...` (warnings); `push: pruned N unregistered token(s)`; `Booking <id>: wrong visit code (attempt n)` (warning, no code in the log).
- **Unlock a visit code by hand:** `UPDATE bookings SET visit_code_failed_attempts = 0, visit_code_locked_until = NULL WHERE id = '...';`
- **Roll back:** `alembic downgrade -1` drops `push_tokens` and the three columns, moves `en_route` bookings back to `accepted`, and rebuilds the enum. Visit codes and tokens are lost. Old code ignores everything else; `start` would work without a code again.

## Follow-ups
- **FE guide examples are generated** from the captures by `scripts/render_fe_guide.py`; a recapture is `capture_caregiver_flow` then `render_fe_guide`.
- **Privacy (fixed 2026-10-06):** `new_requests`, `/caregiver/notifications` and the `decline` response used to return the patient's name, phone, emergency contact, exact address, condition and instructions to caregivers who had only been offered the job. They now return an *offer view* (no patient identity or clinical notes, area-only address, location rounded to ~1 km) until the caregiver is the assigned one (`redact_offer` in `app/api/v1/endpoints/booking.py`, tests `app/tests/test_offer_privacy.py`). Captures re-taken live.
- **Action response shape:** `POST /booking/caregiver/action/{id}` returns a raw dict (money as strings and kobo, internal fields such as `paystack_reference`). A typed response would fix the traps listed in the FE guide.
- **`reason`** on `decline` and `cancel` is accepted and thrown away. **`decline`** does not set `declined`.
- **Push runs inline** in the request that settles the payment (webhook or verify-payment), up to `EXPO_PUSH_TIMEOUT_SECONDS` per batch. Move it to the ARQ worker if Paystack ever times out on slow Expo.
- **Delivery receipts** (Expo's second API) are not polled, so a token that only fails at receipt time is never pruned.
- **Location pings default `is_available` to true** and can switch a caregiver back on; the FE must send the flag every time (documented). Making the field optional ("keep current") would remove the trap but changes the legacy endpoint.
- **Caregiver wallet is never credited** on completion and withdrawals stay `pending` forever (no payout process); both belong to the admin console's pay work.
- **Suspending a caregiver** lists only upcoming `accepted` bookings as affected; an `en_route` booking is not included.
- `profile_status` `suspended` and the invalid-token WebSocket close are covered by tests but not captured live.
- The visit code is a 4-digit value; the lock (3 tries per 15 minutes) is the real control. Moving it to the patient's WhatsApp message (WA-FR-34) is still to do.
