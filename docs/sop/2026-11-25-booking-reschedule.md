# SOP: a patient reschedules a paid booking (Phase 2 sign-off, part E)

| | |
|---|---|
| **What shipped** | `POST /booking/{id}/reschedule` and `GET /booking/{id}/reschedule-terms` for the booker, three admin settings, a reschedule history on the admin booking detail, and `reschedule_count`, `can_reschedule`, `reschedules_left` on the patient's booking views. Branch `feature/booking-reschedule` (cut from `oakly/feature/bank-name-review`, #83); PR number in the PR description; migration `0034_booking_reschedule` (**a placeholder number: the lead re-points `down_revision` at merge time**, the open PRs hold 0031, 0033 and maybe 0035). Not merged. **Needs the Opus review of the price, lock and refund parts before merge.** |
| **Why** | The Phase 2 sign-off pack (part E) says: free reschedule more than 12 h before the visit, 2 free per booking, new time within 28 days (or support adjusts it), re-match automatically and fall back to manual matching, price dynamic at the new time, not offered under 12 h. Until now a patient could only cancel, which can carry a fee. Design and reasons: [plan](../plans/2026-11-25-booking-reschedule.md). |
| **How** | One service, `app/services/booking_reschedule.py`. One transaction under the booking row lock moves the visit, releases the caregiver, closes stale offers and starts a new dispatch wave; the existing wave dispatch and escalation then do the matching, and the admin unmatched queue is the manual fallback. The price is recomputed with `pricing.compute_price`, never changed on the booking, and never moved as money (see Decisions). |
| **Verification** | `test_booking_reschedule.py` (51 tests: both boundaries to the second, the limit, who may, states, caregiver release, stale offers, re-match and the unmatched fallback, price cases, idempotency, three races, HTTP shapes, admin settings and detail), `test_booking_reschedule_migration.py` (7), plus the neighbouring files `test_dispatch`, `test_dropout_redispatch`, `test_admin_bookings`, `test_patient_booking_cancel`, `test_response_shapes`, `test_migrations`, `test_booking_policy`, `test_arrival_policy_migration`, `test_visit_lifecycle`, `test_access_booking`, `test_admin_rbac`, `test_caregiver_cancel_window` (470 tests) all green locally on a throwaway Postgres. Live captures `docs/fe-captures/booking-reschedule/001-025` written by `test_capture_booking_reschedule.py`. **Not done:** the full CI run (CI is the authority), a real device push, a run with the worker cron, anything from the apps. |

## Rules as built

| Rule | Value | Where |
|---|---|---|
| Free window | the visit starts **more than** `reschedule_free_hours` (12) from now; exactly 12 h is refused | `booking_policies.reschedule_free_hours` |
| Free reschedules | `reschedule_max_free` (2) per booking; the 3rd is refused `reschedule_limit_reached` ("contact support") | `bookings.reschedule_count` |
| New time | different, **more than** the free window from now, at most `reschedule_max_days_ahead` (28) days from now (exactly 28 allowed) | `booking_policies` |
| Who | the booker only (the cancel rule) | `patient_booking_cancel.is_booker` |
| Which bookings | paid and `pending` or `accepted`. Unpaid, on the way, started, completed, cancelled, refunded: refused | `booking_reschedule._problem` |

## Decisions

| Decision | Why / alternative rejected |
|---|---|
| **The booking keeps the price it was paid at; nothing moves as money.** | Read from the code: one Paystack reference per booking, refunds go back to it, and a partial refund while the visit is still to come makes `payment_status = partially_refunded`, which breaks accept, dispatch and cancel (all need `paid`). Staff `request_refund` already refuses a live booking for the same reason. Rejected: reprice the booking and queue a refund row (breaks the paid state), a top-up pay link (needs multi-transaction refunds), cancel and rebook (new id and code, the family pays twice). |
| **Lower price: recorded as `credit_due`** on the reschedule row and shown to the family and in the admin detail; Finance refunds it with the existing staff refund once the visit is completed or cancelled. | Safe and honest; manual for now (see Follow-ups). |
| **Higher price: refused** (`price_increase_needs_support`), the terms endpoint already says so, support adjusts. | No silent charge and no second payment path. Rejected: absorb it (silent revenue loss, ignores the owner's "dynamic"). |
| The pricing engine has no time input, so "dynamic at the new time" means the live settings. | `compute_price` takes base fee, multiplier, surge on or off, discount. `get_booking_demand_surge_multiplier` exists but nothing calls it. A difference only appears when an admin changed pricing or switched surge on after the booking was paid. |
| The assigned caregiver is released and **re-offered like anyone else** (the new wave includes them). Not a drop-out: no `caregiver_dropouts` row, no penalty. | The system has no availability calendar, so it cannot know if they can do the new time. Rejected: keep the assignment and ask them to confirm (a new state and timer). |
| Every offer for the old time is closed with response `rescheduled`; **the accept path refuses a `rescheduled` offer.** | A caregiver holding a stale offer must not take the job on the old time's information. Re-offering resets it. One guard added in `BookingService.handle_booking_action`. |
| `dispatch._send_wave(renotify=True)` now looks only at `new_booking` notification rows. | Found while building this: an informational notice row (for example "Visit reassigned") for the same caregiver could be reset to an unanswered one and become a phantom "new request", with no real offer created. Existing bug, also reachable from the drop-out path. |
| The first wave runs in a savepoint; if it fails the reschedule stands and `dispatch_tick` sends it within a minute. | A matching error must not undo the family's change. |
| A booking held for an address check stays held. An open WhatsApp drop-out journey is closed. | Same as when it was first paid; the family chose a new time, so the drop-out choice is moot. |
| Idempotency: `request_id` (unique per booking), or the same target time as the last reschedule; a replay is answered before any rule. | A double tap must never burn the second free reschedule or error after a success. |
| No patient notice event for a patient-made reschedule. | The patient has the answer on screen (like a cancel). An admin-made reschedule needs one (follow-up). |

## What's involved

- `app/services/booking_reschedule.py` (new), `app/api/v1/endpoints/booking.py` (two endpoints, list fields), `app/schemas/booking.py`.
- `app/db/models/booking.py` (`reschedule_count`, `BookingReschedule`), `app/alembic/versions/0034_booking_reschedule.py`.
- Admin: `app/services/booking_policy.py`, `app/db/models/booking_policy.py`, `app/schemas/booking_policy.py` (three settings, validated); `app/services/admin_bookings.py`, `app/schemas/admin_booking.py` (history on the detail).
- `app/services/booking.py` (accept guard), `app/services/dispatch.py` (the `renotify` filter).
- Docs: [design](../plans/2026-11-25-booking-reschedule.md), [FE guide](../fe-integration-guide-patient-booking-reschedule.md), checklist section 19.

### Earnings and arrival-window check (the brief asked for it)
Earnings are only credited at `complete` and for a kept late-cancel fee, so a released caregiver has nothing to void. The arrival window, the on-my-way compensation window and the no-show suspicion read `scheduled_date`, `arrived_at`, `on_my_way_at` and `no_show_flagged_at`: the first moves, the rest are cleared. `scheduled_end_date` (chat and notes windows) moves with it. Tested in `test_an_assigned_caregiver_is_released_told_and_nothing_of_the_assignment_is_left`.

## Operate / roll back

- Deploy: run `0034` (adds a column with default 0, three policy columns with defaults 12, 2, 28, one table). No existing booking changes.
- Settings: `PATCH /admin/booking-policy` (`settings.write`), applied to every booking at once (no per-booking snapshot).
- Finance: bookings with `price_handling = credit_due` are in `GET /admin/bookings/{id}` (`reschedules`). There is no list of them yet.
- Roll back: `alembic downgrade 0030_bank_name_review` drops the table, the count and the settings; rescheduled bookings keep their new times. An app that ignores the new fields keeps working.

## Open questions (for the owner; nothing was guessed silently)

1. **A price increase.** Refused today. Options: absorb it; take a top-up payment (needs a second Paystack transaction and refunds across two transactions); cancel and rebook. Which?
2. **Under 12 h with no caregiver.** The PDF says "not offered under 12 hours" with no exception; for an unassigned booking the cancel is free, so the family can cancel and rebook. Kept as the PDF says.
3. **The new time must be more than 12 h from now** (not in the PDF): so a rescheduled visit never lands in the cancel-fee window and no caregiver gets a last-minute job. Say if a pull-in to a nearer time should be allowed.
4. **The 3rd reschedule** is refused with "contact support" (the PDF is silent). The PDF's "manually adjust upon the client's request" needs an admin-made reschedule, not built here.
5. **Window and the cancel setting are separate** (`reschedule_free_hours` vs `cancel_free_hours`), both 12. The PDF wants them to agree; if an admin changes one, they diverge. Should the reschedule window simply follow the cancel window?
6. **Matching ignores availability** (existing behaviour: it offers to caregivers online now, whatever the visit date), so a visit 20 days out is offered today. Fine for now, worth a look before heavy use.
7. **28 days from now** (the lead's brief) vs the PDF's earlier suggestion "from the original date".
8. The `credit_due` refund is manual. Automating it needs a decision on whether the caregiver's earning is reduced (the refund's take-back is proportional to the refund amount).

## Follow-ups

- Patient app (reschedule screen, hide the option) and admin console (settings, history) in the monorepo.
- Admin-made manual reschedule with a patient notification event and an audit row.
- Open the `credit_due` refund automatically at completion or cancellation.
- Opus review before merge: the price rule, the lock order and the stale-offer guard (see the PR description).
