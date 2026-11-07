# SOP: Patient notifications (in-app feed and push, P8-BE)

| | |
|---|---|
| **What shipped** | App patients now hear about their bookings inside the app, independent of WhatsApp: a `notifications` feed (`GET /patient/notifications`, `POST .../{id}/read`, `POST .../read-all`) and an Expo push to their registered devices, for confirmed/paid, caregiver found / changed / on the way / arrived, visit started / completed, cancelled (by the caregiver or Oakly), no caregiver found, and refund started / completed / needs bank details. |
| **Branch / commit** | `feature/patient-notifications`, cut from `oakly/main` `de0a0fa`. Not pushed; the lead opens the PR. |
| **Migration** | **`0021_patient_notifications`**: table `notifications` (+ two indexes) and two new `booking_events.event` values. Alembic head moves `0020` -> `0021`. The only migration in flight for this slice. |
| **FE guide** | `docs/fe-integration-guide-patient-notifications.md`, rendered from live captures in `docs/fe-captures/patient-notifications/` (`scripts/capture_patient_notifications.py`). |

## Why
The patient app had no notification path at all: the backend told the booker on WhatsApp and the caregiver by push, nothing else. WhatsApp is parked (Meta business verification), and the owner decided the app and WhatsApp coexist, so an app patient must be told through the app. The audit (`LEDGER-patient-audit.md` section 4 and slice P8-BE) asked for the outbox to feed a patient feed and push.

## How
| Area | Decision |
|---|---|
| Reuse the outbox | The existing `booking_events` outbox already has every domain event and the right transactional property (recorded in the booking's own transaction, delivered later by the worker). The app audience is one more branch of that delivery, not a second mechanism. `booking_events.emit` records an event for an **app** booking only if it is in `APP_BOOKER_EVENTS`; a **WhatsApp** booking keeps exactly the events it had and is never recorded `APP_ONLY_EVENTS`; the `admin` channel records no booker event. So a channel is never announced on twice, and a patient is never told on a channel their booking is not on. |
| Delivery | `app/services/patient_notifications.py`: `compose` (wording, with a stale check per event), then **write the feed row and commit**, then push. `_deliver_one` (`app/channels/whatsapp/notifications.py`) sends `channel == "app"` events there instead of to WhatsApp. It runs in the existing worker job (every 15 s), so nothing here runs inside, or can roll back, the booking action. |
| Feed first, push second | The row is committed before Expo is called, so a push that fails, raises, or has no device leaves the row in place. `PushService` already never raises and deletes tokens Expo reports `DeviceNotRegistered` (the same cleanup the caregiver path uses). |
| No duplicates | `notifications.event_id` is unique (the outbox row it came from) and the insert is `ON CONFLICT DO NOTHING`: a delivery retried after a crash writes one row. A failed feed write returns `feed_failed`, the outbox row stays undelivered and is retried (up to the existing 5 attempts), and **no push is sent** until the row exists. |
| Recording can never break the booking | For app bookings `emit` runs its insert in a **savepoint** and logs and swallows a failure (`booking_events.py`), so a broken outbox cannot fail or roll back the caregiver's accept, a payment or a refund. (`db.flush()` runs first so the caller's own pending writes fail as before, not inside the guard.) |
| Lock-screen safe by construction | The only inputs to the wording are a caregiver's first name and the booking's ids. The visit code, address, condition, vitals and even the booking code are never read (a test sets sentinel values for each and asserts none reaches any title, body or push payload). "Arrived" says `Open the app to see your visit code.` `visit_code_issued` is not recorded for app bookings at all (the app shows the code). |
| New events | `caregiver_arrived` and `no_caregiver_found` (app only). `no_caregiver_found` is recorded where dispatch marks a booking `unmatched` (`dispatch._mark_unmatched` now takes the session). |
| A gap fixed on the way | `BookingService._after_caregiver_cancel` returned early for non-WhatsApp bookings and never called `cancelled_by_caregiver`; it now does, and the channel gating in `emit` decides who is told. |
| Token registration | `POST /devices/push-token` is `get_current_user`, so it already works for a patient; no change. |
| Erasure | Account erasure now deletes the user's feed rows next to their push tokens (the feed is personal data). |
| Rejected | **Writing the feed row inline in the booking transaction:** it would put a courtesy write on the money path and the push still needs to happen after commit. **A separate `app_outbox` table:** duplicates claim/lease/retry that `booking_events` already has. **Push-only without a feed:** a missed push would lose the notice. |

## What is involved
| Path | Change |
|---|---|
| `app/alembic/versions/0021_patient_notifications.py` (new) | table, indexes, check constraint swap; downgrade deletes the two new event kinds' rows and drops the table |
| `app/db/models/notification.py` (new) | `Notification` |
| `app/db/models/whatsapp_booking.py` | `caregiver_arrived`, `no_caregiver_found`; `APP_ONLY_EVENTS`, `APP_BOOKER_EVENTS` |
| `app/services/booking_events.py` | channel gating, savepoint guard |
| `app/services/patient_notifications.py` (new) | compose, feed write, push |
| `app/channels/whatsapp/notifications.py` | `channel == "app"` branch in `_deliver_one` |
| `app/services/notification_service.py` | `caregiver_arrived`, `no_caregiver_found` |
| `app/services/dispatch.py`, `app/services/booking.py` | no-caregiver trigger; caregiver-cancel notice for app bookings |
| `app/services/account_erasure.py` | delete the feed on erasure |
| `app/api/v1/endpoints/patient_notifications.py`, `app/schemas/patient_notifications.py`, `app/api/v1/api.py` (new / mounted under `/patient`) | the three routes |
| `app/tests/test_patient_notifications.py`, `test_patient_notifications_migration.py` (new) | 32 + 2 tests |
| `app/tests/test_whatsapp_notifications.py`, `test_whatsapp_admin_notifications.py`, `test_whatsapp_booking_events.py`, `test_dropout_redispatch.py` | four assertions that encoded "an app booking records no booker events" now expect the app notice (and still assert nothing goes out on WhatsApp) |
| `scripts/capture_patient_notifications.py`, `docs/fe-captures/patient-notifications/`, `docs/fe-integration-guide-patient-notifications.md` | live capture and guide |

## Verification
- **Fast checks:** `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke`, both existing `render_fe_guide --check` and the new guide's (see the result lines in the PR description).
- **Tests:** `test_patient_notifications.py` (each journey step through the real booking transitions writes exactly one feed row for the right user and none for others, push called with the right payload, payment, caregiver cancel, Oakly cancel, no caregiver found, refund events, reassignment, arrival, lock-screen text never holds the visit code / address / condition / booking code, a replaced caregiver is not announced, failing and raising push leave the feed row, no-device patient still gets the row, dead token cleanup, feed-write failure retried without a double row or a push, a broken outbox cannot fail the booking action, a WhatsApp booking gets no row and no push and its events are unchanged, feed paging, unread count over the whole feed, ownership, idempotent read and read-all, role and token errors, a patient's own token registration, erasure). `test_patient_notifications_migration.py`: upgrade, downgrade round trip on a scratch Postgres, new events rejected before and accepted after. The modules that share the code (WhatsApp notifications and events, drop-out, dispatch, refunds, admin bookings, account deletion, push) were run: after fixing the four assertions above the rest of them passed (446 passed in the first pass, 4 failed for exactly that reason).
- **Event list pinned at the head:** `test_refunds_migration.py::test_event_check_at_the_migration_head_matches_the_model` upgrades a scratch DB to `head` and asserts `ck_booking_events_event` lists exactly the model's `BOOKING_EVENTS` (the old test compared the model to 0014's frozen snapshot, which 0021 legitimately outgrew; that one is now a subset check).
- **Live:** `scripts/capture_patient_notifications.py` against a real uvicorn process and Postgres: 38 captures. Caregiver actions over real HTTP; delivery by the real function in the capture process; Expo is a local fake.
- **Not verified:** a real Expo push (needs an EAS project, APNs/FCM keys and a device); the worker's 15-second cadence in a deployed environment; a real admin cancel, refund or dispatch give-up driving the events over HTTP (the wording and delivery are verified, the triggers by tests).
- **Review:** one Opus review (read-only) of the diff, no blocking findings. Fixed: the bank-details wording no longer sends the patient to a screen that does not exist; a cancel is not announced for a FAILED payment; `no_caregiver_found` is keyed by the dispatch run so a second give-up is told again; `deliver` reads the event name and booking code before any rollback; an erased (inactive) account gets no new feed rows; the feed row's `created_at` is the event's time. Left as follow-ups below: the rare double "Refund started", the `admin` channel, best-effort drops.

## Operate / roll back
- **Deploy:** run the migration; no new setting. Delivery needs the worker (`whatsapp_events_job`) running, as for WhatsApp; it runs whether or not WhatsApp is configured. Push needs `EXPO_PUSH_URL` (default is Expo's) and, optionally, `EXPO_ACCESS_TOKEN`; without devices only the feed is written.
- **Support:** an event stuck undelivered shows in `booking_events` (`last_error = 'feed_failed'`, `attempts` up to 5). A user's feed is the `notifications` table, by `user_id`.
- **Roll back:** `alembic downgrade 0020` drops the feed (rows are lost) and deletes `caregiver_arrived` / `no_caregiver_found` outbox rows; the code revert restores "an app booking records no booker events". Undelivered app events recorded before a code-only rollback would be marked `no_contact` by the old code, i.e. dropped.

## Follow-ups
- **A caregiver "I've arrived" step** so `caregiver_arrived` has a trigger (the notice, wording and delivery exist and are tested). Needs a caregiver-app action; owner decision on whether arrival is a step.
- Patient cancels (P4-BE) are intentionally not announced; revisit if the owner wants a receipt row.
- `refund_needs_bank_details` says "Our team will contact you" because only the admin console can submit refund bank details; when the app gets a bank-details flow, change the wording and the guide.
- Add the new guide to CI's `render_fe_guide --check` (workflow files are out of scope for this slice).
- Real push: EAS project and APNs/FCM credentials (owner), then one device pass of tap-to-open.
- Retention: feed rows are kept until erasure; decide a retention period.
- `payment_refund_pending` (a late payment on a cancelled booking) shares the `refund_started` wording and can, in a rare path, give two "Refund started" rows for one refund (Opus review, low); merge them if it proves noisy.
- Delivery is best effort: after 5 failed feed writes (about one per 2-minute lease) a notice is dropped; a push can repeat if the outbox row cannot be marked delivered after Expo accepted it (the feed row stays single).
- Booker events for `admin`-channel bookings are not announced anywhere (no known flow produces them).
