# Booking reschedule (patient): design

Written 2026-11-25, before the code. Source of truth: the Phase 2 sign-off pack, part E "Reschedule rules" (the owner's answers in the "Your value" column), and the build map `2026-11-25-clinician-phase2-build-map.md`. This is a money and state-machine path: the sections on lock order, idempotency, price and failure modes are the ones to review.

## 1. The rules (verified against the PDF)

| Rule | Owner's value (PDF part E) | How it is built |
|---|---|---|
| Free window | Free when the visit is **more than 12 hours** away; "all of these become settings in the admin console" | `booking_policies.reschedule_free_hours` = 12. Strictly more than: exactly 12 h is refused (same convention as the cancel tiers, where exactly 12 h is already the fee tier). |
| Free reschedules | 2 per booking | `booking_policies.reschedule_max_free` = 2, counted in `bookings.reschedule_count`. The PDF does not say what the 3rd does: **refused** with `reschedule_limit_reached` and a "contact support" message. |
| Under 12 h | Not offered; the patient cancels with the normal fee | Server refuses `reschedule_window_closed`. The booking list tells the app `can_reschedule = false` so it hides the button. |
| How far ahead | "Make it 28 days, or manually adjust at the client's request" | `booking_policies.reschedule_max_days_ahead` = 28, measured from **now** (the lead's brief; the PDF's older suggestion was "from the original date"). Exactly 28 days is allowed. Beyond: refused `new_time_too_far` with a "contact support" message. |
| Assigned caregiver cannot do the new time | "Match another caregiver automatically... manually find them one if automated matching fails" | The caregiver is released, the booking returns to `pending` and the normal wave dispatch runs again. If nobody accepts, the existing escalation puts it in the admin unmatched queue (manual matching). |
| Price | "Make it dynamic" | Recomputed with the existing pricing engine (section 5). |
| No provider choice, ratings, wallet, drug reminders | owner decisions | not built |

Not in the PDF, decided here (all listed in the SOP under Open questions): the **new time must itself be more than the free window away** (a rescheduled visit never lands inside the cancel-fee window and the caregiver is not given a last-minute job); an **unpaid** booking cannot be rescheduled (nothing is held yet: pay or make a new booking); a visit that is **on the way or in progress** cannot be rescheduled.

## 2. Who, when

Booker only (`is_booker`, the same rule as cancel). A booking is reschedulable when ALL hold, checked under the booking row lock:

1. `payment_status = paid` (not unpaid, not refunded or partly refunded).
2. `status` is `pending` (paid, being dispatched or unmatched, no caregiver) or `accepted` (a caregiver is assigned).
3. Visit start is more than `reschedule_free_hours` away.
4. `reschedule_count < reschedule_max_free`.
5. The new time: different from the current one, more than `reschedule_free_hours` from now, at most `reschedule_max_days_ahead` days from now.
6. The service is still bookable and priced and the length is still allowed (`service_catalog.get_bookable` / `check_duration`, the same checks as creating a booking).
7. The new price is not higher than what was paid (section 5).

## 3. State machine

```
paid, pending (dispatching | unmatched, no caregiver)  --reschedule-->  paid, pending, dispatching (wave n+1, offers reset)
paid, accepted (caregiver C, visit code)               --reschedule-->  paid, pending, dispatching
                                                                        C released, visit code dead, C told (in-app + push)
paid, en_route | in_progress | completed | cancelled   -- refused (booking_not_reschedulable)
```

One reschedule is one transaction under the booking row lock. Inside it:

- booking: `scheduled_date`, `scheduled_end_date = new + duration_minutes`, `reschedule_count + 1`; if assigned: `caregiver_id = NULL`, `status = pending`, `visit_code_enc = NULL`, attempts and lock reset, `on_my_way_at`, `on_my_way_distance_m`, `arrived_at`, `no_show_flagged_*` cleared (the arrival-window and no-show readers key off these; leaving them would let a stale "arrived" or a no-show flag follow the booking); `dropout_state` open states cleared; `dispatch_state = dispatching`, `dispatch_started_at = now` (restarts the 30 minute escalation clock). A booking held for an address check stays held (`dispatch_state` NULL).
- offers: every `new_booking` notification still unanswered or accepted gets `response = "rescheduled"` (the caregiver app lists unanswered offers as new requests, so a stale offer for the old time must go); the new wave then resets the response for the caregivers it picks (`send_wave(..., renotify=True)`). **The accept path refuses an offer whose response is `rescheduled`** (one added guard), so a caregiver holding a stale offer for the old time cannot take the job without being re-offered the new time.
- an audit row in `booking_reschedules` (old and new time, old and new quote, difference, the released caregiver, the request id, the user).
- the released caregiver gets a `booking_update` notice row (response `info`, so it is not a "new request") in the same transaction, and a push after the commit (booking id only, never the patient, address or condition).
- the patient needs no notice: like a cancel they have the answer on screen (and an admin-made reschedule is out of this slice).

Not a drop-out: no `caregiver_dropouts` row, no penalty to the released caregiver, and they may be offered the visit again at the new time (they are in the candidate pool like anyone else).

### Earnings and arrival-window leak check
- Earnings are only credited at `complete` (`earnings.credit_visit`) and for a kept late-cancel fee; nothing is credited at accept, so releasing a caregiver leaves no earning to void. The booking's `earnings_terms` snapshot is kept (the price is not changed, section 5).
- The arrival window (`ARRIVAL_EARLIEST_MINUTES_BEFORE`), the on-my-way compensation window and the no-show suspicion all read `scheduled_date`, `arrived_at`, `on_my_way_at` and `no_show_flagged_at`; the first is updated and the others cleared above. `scheduled_end_date` feeds the chat and notes windows (`visit_windows.ended_at`) and is updated too.
- A caregiver whose booking was released loses access by the existing rule that chat, notes and client views key off `booking.caregiver_id`.

## 4. Lock order and concurrency

Lock order is the project's: **booking row, then (refund, caregiver rows)**. Reschedule takes the booking row `FOR UPDATE` first and never takes a caregiver lock (accept takes booking then `caregiver FOR SHARE`; reschedule has no caregiver lock, so no cycle). Notifications and wave rows are written under the booking lock.

| Race | Outcome |
|---|---|
| reschedule vs caregiver accept | They serialise on the booking lock. Accept first: the booking is `accepted`, reschedule releases that caregiver and re-dispatches. Reschedule first: the caregiver's stale offer is `rescheduled` and the accept is refused (403 "This visit was rescheduled"); the booking is open for the new wave. |
| reschedule vs cancel | Serialise. Cancel first: reschedule sees `cancelled` and refuses 409. Reschedule first: the cancel then prices the cancel tier on the NEW time (after a reschedule the booking is 12 h+ away, so free). |
| reschedule vs reschedule (double tap, two devices) | Serialise. The second finds either the same `request_id` or the same target time already applied and **replays** the stored result (200, `already_rescheduled: true`): no second count, no second wave. A different target time is a normal second reschedule and counts. |
| reschedule vs dispatch tick | The tick takes `skip_locked`, so it skips a locked booking and sees the new state next minute. |
| reschedule vs admin reassign/cancel/redispatch | Same booking lock; each re-checks state under it. |

## 5. Price (needs an Opus review)

What the engine does: `pricing.compute_price` from the latest `booking_settings` (base fee, service multiplier, surge on/off and multiplier, discount, platform fee). **It has no time-of-day or date input**: surge is a global switch. So "dynamic at the new time" means "recompute with the settings that apply when the patient reschedules". A difference only appears when an admin has changed pricing (or switched surge) since the booking was paid.

What the money machinery forces (read from the code, not assumed):

1. A booking has **one Paystack reference** and refunds go back to that one transaction. A second (top-up) payment would need multi-transaction refunds, a second webhook path and an expiry sweep. Too large and too risky for this slice.
2. A **partial refund while the booking is live breaks the state machine**: `refunds._mark_succeeded` sets `payment_status = partially_refunded`, and accept (`payment_status == paid`), dispatch (`_is_open`) and `cancellation.terms` (`payment_status == paid`) all require `paid`. So a "refund the difference" row cannot be approved while the visit is still to come. The refund queue's own rule agrees: staff `request_refund` is only allowed for completed or cancelled visits.

So the safest reading, chosen:

| New price vs paid | What happens |
|---|---|
| equal | Reschedule applies. |
| **lower** | Reschedule applies. **The booking keeps the price it was paid at** (no repricing: the refund and earnings maths all read `total_amount_kobo`/`service_fee_kobo` and stay consistent). The difference is **recorded** on the reschedule row (`price_difference_kobo < 0`, `price_handling = credit_due`) and shown to the patient ("you paid X, the price now is Y, the difference will be refunded after the visit") and to admins in the booking detail. Finance settles it with the existing staff refund (`request_refund`, allowed once the visit is completed or cancelled). No money moves at reschedule time. |
| **higher** | **Refused** (409 `price_increase_needs_support`, with the quote). No charge is ever taken silently and no second payment is needed. Support can still help (the PDF's "manually adjust" path). |

The app shows the quote before the patient confirms and sends back `accepted_total_kobo`; if the price decided under the lock differs, nothing changes and the answer is 409 `price_changed` with the new quote (the same shape as the cancel `terms_changed`).

Alternatives rejected: (a) absorb the increase (silent revenue loss, ignores the owner's "dynamic"); (b) a top-up pay link (needs multi-transaction refunds first); (c) cancel and rebook behind the scenes (new booking id and code, the family pays twice and waits for the refund); (d) reprice the booking and queue a refund row (breaks `payment_status`, see 2.).

## 6. Idempotency

- `request_id` (optional client UUID, at most 64 chars): `UNIQUE (booking_id, request_id)` (partial, where not null). A replay returns the stored result.
- Without it, a request whose target equals the current `scheduled_date` replays the latest reschedule when that one ended at the same time, else 422 `same_time`. So a double tap never burns a second free reschedule and never errors after a success.
- A replay is answered before every eligibility rule (after the first success, the count or window may no longer allow the same call).

## 7. Failure modes

| Failure | Behaviour |
|---|---|
| Wave sending raises (candidate query error) | Caught in a savepoint and logged; the reschedule stands, `dispatch_state = dispatching`, and `dispatch_tick` (every minute) sends the next wave. |
| Nobody online / nobody accepts | Existing escalation: `unmatched` after `DISPATCH_ESCALATE_MINUTES` (or at once when no candidates remain after a wave), and the booking shows in the admin unmatched queue; the booker gets the existing `no_caregiver_found` notice. |
| Push fails | Logged, never undoes the reschedule (the notice row exists and the app polls). |
| Booking settings missing | 503 `booking_settings_not_configured` (same as creating a booking), nothing changed. |
| Any error inside the transaction | Roll back: nothing changed, retry with the same `request_id`. |
| Policy edited between the preview and the confirm | Decided live under the lock; the answer reflects the live policy. |

## 8. API (patient app, `/api/v1/booking`)

- `GET /{id}/reschedule-terms?new_scheduled_date=...` (target optional): eligibility, limits, the allowed range, remaining count, and (with a target) the price quote. Always 200 for the booker; `can_reschedule` false with a `reason`.
- `POST /{id}/reschedule` body `{new_scheduled_date, request_id?, accepted_total_kobo?}`.
- Errors use `detail = {code, message, ...}` like cancel: 403 `not_your_booking`, 404 `booking_not_found`, 409 `booking_not_reschedulable` / `booking_unpaid` / `reschedule_window_closed` / `reschedule_limit_reached` / `price_increase_needs_support` / `price_changed` / `service_unavailable`, 422 `new_time_too_soon` / `new_time_too_far` / `same_time`, 503 `booking_settings_not_configured`.
- The patient booking list gains `reschedule_count`, `can_reschedule`, `reschedules_left` (so the app can hide the option).
- Admin: `GET/PATCH /admin/booking-policy` gains the three settings; the admin booking detail gains `reschedule_count` and a `reschedules` history (time, price quote, difference, released caregiver).

## 9. Out of this slice (next)

Admin-made manual reschedule (the PDF's "manually adjust" for beyond 28 days or past the limit), with a patient notice event; automatic opening of the credit refund at completion (needs a decision on the caregiver take-back for a repriced refund); patient app screens and the admin console fields.
