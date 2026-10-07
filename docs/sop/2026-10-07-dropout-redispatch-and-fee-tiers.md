# SOP: caregiver drop-out re-dispatch, two-tier cancellation fee, admin-editable booking policy

| | |
|---|---|
| **Date** | 2026-10-07 |
| **Branch** | `feat/whatsapp-dropout-redispatch-fee-tiers` (rebased onto `origin/main` at `8226ea1`, which includes PRs #28 and #29) |
| **Migration** | `0010_dropout_fee_tiers` (down_revision `0009_account_erasure`, the account-erasure migration from PR #29) |
| **Decisions** | Owner, 2026-10-06 (drop-out and fee tiers), and the later change that the policy is edited in the admin console, not the env file |
| **Status** | Verified locally: unit/integration tests, CI checks, the Docker end-to-end script and a live capture of the new admin routes. Not pushed. **Not run against real Meta.** See "Result". |

## What shipped
- **A caregiver drop-out on a WhatsApp booking is replaced automatically** instead of refunded (W3 refunded). App bookings are unchanged.
- **Two-tier family cancellation fee** replacing the single 20%: free, then 20%, then 50% of the service fee, with the tier stored on the booking.
- **`late_cancel_compensable`**: whether a late cancellation followed a *qualifying* on-my-way (A3 will pay the caregiver from it; nothing is paid here).
- **The policy is data, not env**: a `booking_policies` row an admin edits (`GET`/`PATCH /api/v1/admin/booking-policy`), with a reason and an audit row. `WHATSAPP_CANCEL_FREE_HOURS` and `WHATSAPP_CANCEL_FEE_PERCENT` are gone from Settings and `.env.example`.
- **Caregiver drop-outs are recorded** in `caregiver_dropouts` (any channel), for the admin console to show later.

## Why
A caregiver dropping out of a paid visit left the family with a refund and a booking to redo. Most drop-outs happen with time to spare, so a replacement is usually found before the family needs to do anything; when there is not time they need a clear choice, never a fee. The single 20% fee also ignored how close the visit was. The owner wanted both rules adjustable without a deploy, and a family never to meet terms they had not agreed to.

## How

### The drop-out (`app/services/dropout.py`)
```
caregiver cancels (accepted or on the way), paid WhatsApp booking
 |- more than dropout_redispatch_min_lead_minutes (120) before the start, not yet on the way
 |     -> reopened (pending, no caregiver, visit code cleared), re-offered by the normal dispatch to everyone eligible
 |        EXCEPT the caregiver who dropped; booker told "finding you another one, no action needed"   [redispatching]
 |        |- a caregiver accepts -> the usual caregiver_assigned + visit_code events (keyed per issuance); state cleared
 |        '- nobody dropout_decision_lead_minutes (60) before the start -> the choice (worker, every minute at second 20)
 '- closer than that, or on the way -> reopened and re-offered the same way, and the choice at once       [awaiting_choice]
choice (three stateless buttons, bk:wait|resched|refund:<booking id>)
   Keep waiting      -> [waiting]  dispatch carries on, NOT escalated to "unmatched" after 30 min; at the start time with nobody -> handed over
   Pick another time -> [handed_over]  open offers closed, support ticket (talk_to_person), chat to a person, booking shows in the admin unmatched queue
   Full refund       -> cancelled, refund_required, refund_amount_kobo = amount paid (no fee), [closed]
unanswered choice, or waiting, at the start time -> handed over (ticket, chat to a person, booker told by booking_update)
```
`bookings.dropout_state`: `redispatching | awaiting_choice | waiting | handed_over | closed`.

| Decision | Choice | Alternatives rejected |
|---|---|---|
| Where the reopen happens | `BookingService.handle_booking_action` "cancel", under the booking lock, in the same transaction as the drop-out record, the new wave and the booker event | Reopening from a worker: a window where the caregiver's cancel is visible but nothing is happening |
| The caregiver's own cancel response | The returned booking is now `pending` (not `cancelled`) for a WhatsApp booking dropped before the visit | A separate status: changes the mobile enum. The caregiver app lists by `caregiver_id`, so the booking simply leaves their list |
| Never back to the same caregiver | `caregiver_dropouts` is checked in `dispatch._send_wave` (every wave, also admin re-dispatch) and in accept (403 "You cancelled this booking") | Marking their notification: `renotify` resets notifications |
| First wave after the reopen | `renotify=True`: earlier "no" answers get another chance | Only never-notified caregivers: a small pool would be exhausted at once |
| Immediate choice also dispatches | Yes: a replacement accepting meanwhile makes the buttons stale ("already dealt with") | Pausing dispatch until they answer: slower, and no benefit |
| Automatic replacement and "Keep waiting" | `dispatch._advance` skips the 30-minute escalation and the "no candidates left" exit for `redispatching` and `waiting`, so a replacement is looked for until the decision lead (or the start time); the 30 minutes start again when the family is asked (`dispatch_started_at` reset) | A long escalation setting: would change every booking |
| Time-driven step | New ARQ cron `dropout_tick_job` (every minute, at second 20, never in the same second as the jobs that lock bookings: it skips a locked booking and a same-second start would hide the same booking from it every minute) -> `dropout.tick`: conversation lock, then booking (skip-locked), re-checked under the lock | Piggy-backing on `dispatch_tick`: that job only sees `dispatching` bookings |
| Unanswered choice at the start | Handed over (not specified; the alternative is a booking nobody owns) | |
| Pick another time | A ticket and a person; rescheduling is not built | |
| Health data | None in any new message: booking code, the three button labels | |
| Pushes | Job-offer pushes are returned from the transaction and sent after the commit (`dispatch.push_offers`); no HTTP under a lock | |

Lock order is the existing one: conversation, then booking (the choice runs inside the chat step that already holds the conversation lock; `tick` takes them in that order); the booking is locked before any caregiver row.

### The fee tiers (`app/services/booking_cancellation.py`)
| Booking | Tier | Result |
|---|---|---|
| paid, nobody assigned | `none` | full refund |
| accepted, more than `cancel_free_hours` (12) before the start | `none` | full refund |
| accepted, between `cancel_late_hours` (2) and `cancel_free_hours` | `standard` | fee = `cancel_fee_percent` (20) of the service fee |
| accepted, less than `cancel_late_hours` before the start (exactly 2 h is still `standard`) or past it; or the caregiver is on the way and tapped it within `onmyway_compensation_window_minutes` of the start | `late` | fee = `cancel_late_fee_percent` (50) of the service fee |
| in progress | | refused, handed to a human (unchanged) |

W3's fee-bound confirm button is unchanged (`bk:confirm:<id>:<fee_kobo>`, recomputed under the lock, re-asked if higher). The cancellation policy text and the quote's cancellation line follow the policy. `bookings.cancel_fee_tier` stores the tier so A3 can split the fee.

**Snapshot.** When the quote is made, the cancellation tiers are copied into `quotes.payload.cancel_policy`; confirming the quote copies them onto `bookings.cancel_policy`. Cancellation and the policy text read the booking's copy, so an admin edit never changes terms a family agreed to and the terms shown always match the charge. Bookings with no copy (app bookings, admin-created, quotes older than this change) use the live policy. Drop-out timings and the on-my-way window are read live.

### `late_cancel_compensable`
**On my way is only accepted from `onmyway_compensation_window_minutes` (120) before the start**: earlier, the caregiver gets `409` "You can mark yourself on the way from 2 hours before the visit" (app bookings too; the hours follow the policy). Otherwise a caregiver could tap days ahead and make the family pay 50%. As a second guard, EN_ROUTE makes the tier late only when `on_my_way_at` is within that window of the start; an en-route booking with an earlier or missing tap falls back to the clock tiers.

Decided under the booking lock at cancel time, late tier only (`NULL` otherwise): the tap was no earlier than `onmyway_compensation_window_minutes` (120) before the start, **and** the caregiver's latest location is newer than the tap and at least 300 m closer to the patient than when they tapped (`app/services/onmyway.py`, `MIN_PROGRESS_M`). The tap time and the distance at the tap are stored when the caregiver taps (`bookings.on_my_way_at`, `on_my_way_distance_m`), because only the latest ping is kept per caregiver. No location on file at the tap, or one older than 5 minutes at the tap (`LOCATION_FRESH_MINUTES`), means no distance and so `false`. **Location pings are client-reported (the caregiver's phone says where it is), so the movement check is best-effort, not proof, until there is a location history table.**

### The policy (`booking_policies`, admin API)
A separate single-row table (`id = 1` enforced) rather than columns on `booking_settings`: that table is a history of *price* rows used by the app and the quote, writable by `pricing.write` with no reason or audit; these are rules with their own permission and audit trail, and one current value.

| Field | Default |
|---|---|
| `cancel_free_hours` | 12 |
| `cancel_late_hours` | 2 |
| `cancel_fee_percent` | 20 |
| `cancel_late_fee_percent` | 50 |
| `dropout_redispatch_min_lead_minutes` | 120 |
| `dropout_decision_lead_minutes` | 60 |
| `onmyway_compensation_window_minutes` | 120 |
| `caregiver_late_fee_share_percent` | 80 (stored now, used by A3) |

Validated in the service **and** by database check constraints: percents 0 to 100, hours 0 to 168, minutes 0 to 10080 (anything larger is a `422`, never a database error), `cancel_late_fee_percent` not lower than `cancel_fee_percent`, `cancel_late_hours < cancel_free_hours`, `dropout_decision_lead_minutes < dropout_redispatch_min_lead_minutes`. `GET` needs `bookings.read`; `PATCH` needs `settings.write` (Operations and Super Admin), a non-blank `reason`, at least one field, and writes an `admin_audit_log` row (`booking_policy.update`, old and new values, reason). Errors are `{"detail": ...}` like the other admin routes.

## What's involved
- **Migration** `app/alembic/versions/0010_dropout_fee_tiers.py`: `booking_policies` (seeded), six `bookings` columns, `caregiver_dropouts`, three `booking_events` events, a partial index for the worker scan.
- **Models** `app/db/models/booking_policy.py`, `dropout.py` (new), `booking.py`, `whatsapp_booking.py`.
- **Services** `booking_policy.py`, `dropout.py`, `onmyway.py` (new); `booking_cancellation.py`, `booking.py` (accept guard, on-my-way record, cancel hook, pushes after commit), `dispatch.py` (exclude dropped, waiting mode), `notification_service.py`, `admin_bookings.py` (an assign clears the drop-out state).
- **Channel** `templates.py` (`choice_buttons`; two templates), `booking_notices.py`, `engine.py` (`DropoutChoice`), `my_bookings.py`, `booking_chat.py` (snapshot, quote line), `copy.py`.
- **API** `app/api/v1/endpoints/admin_booking_policy.py`, `app/schemas/booking_policy.py`; **worker** `app/worker.py` (`dropout_tick_job`).
- **Config** `app/core/config.py` and `.env.example`: the two `WHATSAPP_CANCEL_*` settings removed (Settings forbids unknown keys, so a deployed env file that still sets them must drop them).
- **Tests** `test_booking_policy.py`, `test_cancellation_tiers.py`, `test_dropout_redispatch.py`, `test_onmyway_movement.py`, `policy_helpers.py`; updated `test_whatsapp_my_bookings.py`, `test_whatsapp_booking_chat.py`, `test_whatsapp_notifications.py`, `test_whatsapp_templates.py`, `test_worker.py`, `test_admin_rbac.py`.
- **Tooling and docs** `scripts/whatsapp_local_e2e.py`, `scripts/capture_admin_flow.py` (`--section policy`), `docs/fe-integration-guide-admin.md` and captures.

### New templates (add to the owner list in the [W3 SOP](2026-10-06-whatsapp-notifications.md#owner-steps-nothing-here-has-been-done))
| # | Template | Body | Essential | Buttons |
|---|---|---|---|---|
| 13 | `dropout_replacing` | Booking {{1}}: your professional had to cancel. We're finding you another one, so there's nothing you need to do. Reply here if you need help. | yes | none |
| 14 | `dropout_choice` | Booking {{1}}: your professional had to cancel and we haven't found a replacement yet. What would you like to do? | yes | Quick replies: Keep waiting, Pick another time, Full refund. Each payload is set per message (`bk:wait|resched|refund:<booking id>`) |

A handed-over booking reuses `booking_update` (the sentence "We couldn't find a replacement in time, so our team is now looking after your booking and will contact you here."). All Utility, English.

## Verification
| Check | Result |
|---|---|
| Full pytest with CI's env values, on the tree rebased onto `main` at `8226ea1` (PR #29) | **1926 passed** on the final tree (one unrelated rate-limit-window test, `test_admin_auth_review.py::test_spoofed_xff...`, failed once in the full run and passed 3 of 3 alone) |
| CI steps: `poetry check --lock`, `ruff --select E9,F63,F7,F82`, `scripts.ci_smoke`, both `render_fe_guide --check` | all clean |
| Migration: single head `0010`, upgrade from 0009 with seed row, DB check constraints refuse bad values and a second policy row, downgrade then upgrade again (`test_dropout_migration.py`), plus the whole-history round trip (`test_migrations.py`) | pass |
| Docker end to end `python -m scripts.whatsapp_local_e2e --up --down` (compose project `oakly-dropout`, fake Meta and Paystack) | **293 checks passed** on the final rebased tree (after the review fixes; `docker compose` project `oakly-dropout`) |
| Live admin captures for the policy routes (`capture_admin_flow --section policy`, captures 119 to 131), rendered into `docs/fe-integration-guide-admin.md` section 12.9 | done |

**Mutation checks** (one guard removed at a time, the policy, cancellation, drop-out and My bookings tests run, each must fail): en-route-is-late, the 2 h line, the 12 h line, late percent, snapshot ignored, `late_cancel_compensable` always true, window ignored, ping-after-tap ignored, distance ignored, fee-bound confirm removed, the booking lock in `choose`, booker check, stale-state check, refund amount, dropped caregiver excluded from waves, accept guard, `en_route` ignored, hand-over at the start, policy validation, audit action name, and the no-30-minute-escalation for `redispatching`. Three survived at first (booker check, stale check, exclusion) and each got a test that now kills it: the exclusion test's dropping caregiver had no location so was never a candidate, and `dropout.choose` was only exercised through the chat's own checks. On the base (`1daa634`) the new and updated tests fail: 6 modules cannot import and 19 tests fail.

**Found and fixed after the money review**
- On my way is refused earlier than the compensation window, and EN_ROUTE only forces the late tier for a tap inside it (a caregiver tapping a day early used to cost the family 50%).
- The distance at the tap uses the caregiver's last fix only if it is at most 5 minutes old.
- The late tier starts at *less than* 2 h (exactly 2 h is the 20% tier).
- "Pick another time" and the hand-over at the start time close the open offers, as the refund does.
- Policy upper bounds (hours 168, minutes 10080) and late fee >= fee, in the service and the database; an oversized integer is a `422`. The existing policy captures are unchanged (their messages did not change); the new rules are covered by tests, not recaptured.

**Found and fixed while verifying**
- Re-dispatch stopped after 30 minutes (the usual escalation) although the plan is to keep looking until the decision lead. Now `redispatching` is exempt like `waiting`.
- `dropout_tick_job` started in the same second as `dispatch_tick_job`, which locks the same bookings; the tick skips locked rows, so the same booking could be missed minute after minute. It now runs at second 20.
- After the rebase onto PR #29 (patient address columns can now be null for an erased patient) the movement check treats a missing address as "not compensable" instead of failing.
- e2e: the final stuck-event check ran before the worker delivered the last events (it now waits up to 60 s and prints the rows), and the "visit code not in the log" check matched four digits inside UUIDs by chance (it now looks for the code as a standalone token; this was a pre-existing flake in the W3 check).

**Not verified.** Nothing was run against real Meta or real Paystack. The two new templates were not submitted. One e2e run in seven timed out waiting for the choice message in the "3 hours to go" scenario before the cron-offset fix above; it was not reproduced afterwards (it is consistent with the same-second lock collision).


## Operate / roll back
- **Change the policy:** `PATCH /api/v1/admin/booking-policy` with a reason (Operations or Super Admin). Takes effect for bookings quoted afterwards (cancellation tiers) and immediately for drop-out timings. Read it: `SELECT * FROM booking_policies;`.
- **See drop-outs:** `SELECT caregiver_id, count(*) FROM caregiver_dropouts GROUP BY 1 ORDER BY 2 DESC;` and `SELECT booking_code, dropout_state FROM bookings WHERE dropout_state IS NOT NULL;`.
- **Stuck choice:** a booking in `awaiting_choice` is handed to a person at the start time by the worker. An operator can also assign or cancel it from the unmatched queue (A2); assign clears the state.
- **Roll back:** `alembic downgrade -1` drops the table, the columns and the three events. **It destroys data**: the whole drop-out record (`caregiver_dropouts`), every booking's stored cancellation snapshot, tier, compensable flag, on-my-way time and distance and drop-out state, the edited policy values (back to env-less code that has none), and the three notification events (pending ones are deleted). Take a database backup first if anything has been recorded since the deploy. Deploy migration, API and worker together; remove `WHATSAPP_CANCEL_*` from any env file (the old code would still read them, the new Settings rejects them).
- **Mobile contract:** the caregiver `cancel` response for a WhatsApp booking dropped before the visit now has `status: pending` (the booking was reopened). Tell the caregiver app team. Also new for the caregiver app: `on_my_way` returns `409` "You can mark yourself on the way from 2 hours before the visit" earlier than the policy window, for app bookings too (hide the button until then).

## Follow-ups
- **A3:** pay the caregiver from `late_cancel_compensable`, `cancel_fee_tier` and `caregiver_late_fee_share_percent`; call `NotificationService.refund_processed` on payout.
- **Admin console:** show `caregiver_dropouts` per caregiver; a policy page over the new routes.
- **The existing `booking-settings` admin routes** (`POST`/`PUT`/`DELETE`, `pricing.write`) take no reason and write no audit row. Contract left as it is, flagged for the owner.
- Choice taps are handled when the chat is idle (like W3's My bookings buttons); a person mid-way through another step is shown the menu. Revisit if it matters in practice.
- Rescheduling ("Pick another time") is a ticket and a person for now.
- No location on file at the tap means a late cancel can never be compensable; a location history table would let the check use the first ping instead.
- Submit the two templates (13, 14) to Meta; check the three quick-reply payload buttons are accepted as dynamic payloads.
