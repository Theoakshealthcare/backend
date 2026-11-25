# SOP: post-visit windows (chat, typed notes) and last-known-location matching with a stale-location nudge

| | |
|---|---|
| **What shipped** | (1) Matching uses a caregiver's last known location for up to 6 h. (2) An ARQ cron pushes "Update your location" to available caregivers whose fix is older than 6 h. (3) A booking chat is read-only 24 h after the visit is completed (and at once when cancelled), with a stable `chat_closed` error. (4) Typed visit notes follow the voice-note windows. |
| **Branch / PR** | `feature/post-visit-windows-location`, cut from `oakly/develop` `fece30b`. One PR on `Theoakshealthcare/backend`; not merged (waits for the owner's "merge #N" and green CI). |
| **Migration** | `0029_location_nudged_at` (one nullable column). Head is now `0029_location_nudged_at` (one head). |
| **FE guide** | [`docs/fe-integration-guide-post-visit-windows-location.md`](../fe-integration-guide-post-visit-windows-location.md), rendered from `docs/fe-captures/post-visit-windows-location/`; checked in CI. |

## Why
The owner's first real-phone test (2026-11-25) approved four behaviours. The app reports location only while it is open, so an available caregiver who pockets the phone silently stopped receiving offers after 15 minutes. Chat had no status or time condition at all (a closed visit's chat stayed writable forever). Typed notes had no window, while voice notes already had one.

## How
| Decision | Why / alternative rejected |
|---|---|
| **`AVAILABLE_LOCATION_MAX_HOURS=6` replaces the 15-minute test in `LocationService.find_nearby_caregivers`.** Everything else in that query (available, active row, approved, not suspended, account active, skill, radius) is unchanged. | The task said to keep `LOCATION_FRESHNESS_MINUTES` for tracking "unless it is the same check". Reading the code: `LOCATION_FRESHNESS_MINUTES` was used **only** by this matching query; the on-my-way check has its own constant (`LOCATION_FRESH_MINUTES = 5` in `app/services/booking.py`) and live tracking has no freshness rule. So nothing needed separating; the setting is kept and now means "fresh enough to rank first". |
| **Ranking: a fix older than `LOCATION_FRESHNESS_MINUTES` loses 10 match-score points** in `get_nearby_active_caregivers_with_eta`; `location_age_minutes` and `last_location_update` added to each candidate. | Simple and small: at equal distance a fresh fix wins, but a closer stale caregiver still outranks a far fresh one (distance is worth up to 50 points). Alternative rejected: sorting by age first, which would hide the closest caregiver. |
| **Nudge state on `caregiver_locations.location_nudged_at`** (the location row), set only when Expo accepted the push. | The nudge is about the location row; one row per caregiver. A failed push is not recorded, so the next 15-minute run retries it. The update names `updated_at` in its SET (to itself) so the column's `onupdate` cannot refresh it: `updated_at` is the time of the last fix, and a nudge must never make a stale fix look fresh (same trick as `caregiver_home.set_availability`). |
| **Cron every 15 minutes (`minute={4,19,34,49}, second=35`)**, 500 caregivers per run. | A stale fix is hours old, so minute precision is pointless, and a failed Expo call is retried at most four times an hour. Offset from the other jobs' seconds. |
| **"Active visit" = `en_route` or `in_progress`.** | An `accepted` visit still in the future is not "active" and the caregiver is idle. Easy to widen (`BUSY` in `app/services/location_nudge.py`). |
| **Quiet hours 21:00 to 07:00 `HOME_TIMEZONE`** (`in_quiet_hours`, may cross midnight; start inclusive, end exclusive). Nothing is recorded during the quiet hours, so the nudge goes out when they end. | Settings `LOCATION_NUDGE_QUIET_START_HOUR`/`END_HOUR`; `LOCATION_NUDGE_MIN_GAP_HOURS=24`. |
| **Chat window in one module, `app/services/visit_windows.py`**: open while not terminal, and until `ended_at + CHAT_OPEN_HOURS_AFTER_VISIT` once completed; `cancelled` and `declined` closed at once. `ended_at` is `actual_end_time`, else `scheduled_end_date`, else `scheduled_date` (never `updated_at`, which any edit would stretch). | One definition for the REST send, the payload fields and the chat list. Reading history is never gated. |
| **Error: `409 {"detail": {"code": "chat_closed", "message": ...}}`**, checked after the 404/403 access checks, before the message is saved (so no push). | 409 (a state conflict), not 403 (which the apps already read as "not your booking"). Same `{code, message}` shape as the other stable errors. |
| **Typed notes share the voice-note rules through `visit_windows.can_start_note` / `can_read_notes`**; `voice_notes/access.py` now calls them (behaviour unchanged, voice tests pass). | "Same windows through the same helper". |
| **Care-plan observations are left alone.** | They are already stricter (add only during `in_progress`, append-only) and a caregiver's plan access ends when the booking completes (`care_plan_access` module doc); a grace period there needs plan access after completion, a privacy decision for the owner. Reported, not changed. |
| **Exposure:** `chat_open`, `chat_closes_at` on `BookingResponse` (every enriched booking payload) and on each item of `GET /caregiver/chats`; `notes_open`, `notes_closes_at`, `notes_readable_until` only on the assigned caregiver's own booking payloads. | The caregiver chat list was the one chat-list endpoint; the patient's chat screen reads the booking. |

## What's involved
| Path | Change |
|---|---|
| `app/core/config.py` | `AVAILABLE_LOCATION_MAX_HOURS`, `LOCATION_NUDGE_MIN_GAP_HOURS`, `LOCATION_NUDGE_QUIET_START_HOUR`, `LOCATION_NUDGE_QUIET_END_HOUR`, `CHAT_OPEN_HOURS_AFTER_VISIT` |
| `app/services/location.py` | eligibility window, ranking, `last_location_update`, `location_age_minutes` |
| `app/services/location_nudge.py` (new), `app/worker.py` | the nudge and `location_nudge_job` |
| `app/alembic/versions/0029_location_nudged_at.py`, `app/db/models/booking.py` | `caregiver_locations.location_nudged_at` |
| `app/services/visit_windows.py` (new) | chat and note windows, `chat_closed_error` |
| `app/services/voice_notes/access.py` | uses the shared note windows |
| `app/api/v1/endpoints/booking.py`, `app/schemas/booking.py` | `send_chat_message` 409, payload fields, booking-notes windows |
| `app/services/caregiver_chats.py`, `app/schemas/caregiver_chat.py` | list item fields |
| `deploy/env.example` | the five new settings (code defaults apply; `.env.*.enc` untouched) |
| `.github/workflows/ci.yml` | the new guide's render check |

## Verification
- Tests (named files only; CI runs the full suite): `test_dispatch.py` (10 min, 5 h and 7 h fixes; unavailable, unapproved and suspended stay out; stale ranks below fresh at equal distance), `test_location_nudge.py` (message, once per 24 h, quiet hours, busy, no token, failed push, `updated_at` unchanged), `test_location_nudge_migration.py` (round trip on a scratch DB, one head), `test_worker.py` (cron registered), `test_chat_and_notes_windows.py` (24 h boundary, cancelled, both senders, no save and no push, history, fields, typed-note windows), `test_caregiver_chats.py`, `test_chat_push.py`, `test_voice_notes_api.py`, `test_access_booking.py`, `test_patient_booking_cancel.py`, `test_visit_lifecycle.py`.
- The FE guide is rendered from real responses captured on the test harness (`CAPTURE_POST_VISIT_WINDOWS=1`).
- **Not exercised:** the cron firing on schedule and a real push reaching a phone (no live Redis worker or Expo here).

## Operate / roll back
- No setup needed: the code defaults are the agreed values. Tune with the env settings above.
- Roll back by reverting the PR. The migration downgrade drops `location_nudged_at` (bookkeeping only); the chat and note rules are code only.

## Follow-ups
- **No chat WebSocket exists:** `app/api/v1/endpoints/websocket.py` is not mounted and does not import (it needs `decode_websocket_token`, which does not exist). The "websocket send" cannot be enforced because it cannot run; if a live chat socket is built it must call `visit_windows.chat_window`.
- Care-plan observations keep "in progress only" (see How); extending them to the 2 h grace needs a plan-access decision.
- The mobile apps must adopt the new fields and the `location_stale` push (guide sections 1 to 3).
