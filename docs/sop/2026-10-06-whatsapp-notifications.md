# SOP: WhatsApp notifications and "My bookings" (WP-W3)

| | |
|---|---|
| **Date** | 2026-10-06 |
| **Branch** | `feat/whatsapp-notifications` (base `main` at `aade294`, which includes W1 and W2) |
| **Migration** | `0006_whatsapp_notifications` (down_revision `0005_whatsapp_booking`). **Renumber at merge if Admin A2 lands a migration first.** |
| **Plan / spec** | [plan W3](../plans/2026-10-06-whatsapp-channel.md) · [PRD](../prd/PRD-whatsapp-patient-channel.md) WA-FR-30 to WA-FR-39, WA-NFR-6 |
| **Status** | Verified locally: unit/integration tests, CI checks, and the Docker end-to-end script against a fake Meta and a fake Paystack. Not pushed. **Not run against real Meta.** |

## What shipped
- **`NotificationService`** (`app/services/notification_service.py`): one named method per domain change ("the caregiver accepted", "the visit started"...). Each writes outbox rows (`booking_events`) **in the same transaction as the change** and never sends. A worker delivers each row to its audience: the booker on WhatsApp, the caregiver by Expo push.
- **The visit's journey to the booker** (WA-FR-30 to 37): caregiver assigned, visit code, on my way, visit started, visit completed; plus cancelled by the caregiver or an admin, refund started, refund done.
- **Visit code to the booker**, without health data (WA-NFR-6).
- **My bookings in chat** (WA-FR-38): the 5 most recent bookings for the contact, their care recipients and their own app bookings; a detail view with actions that follow the status.
- **Cancelling in chat** (WA-FR-39): the policy is shown, then applied; refund goes through the existing `refund_required` queue; unpaid bookings reuse W2's verify-before-cancel.
- **Caregiver push** when a booking they hold is cancelled by the booker or an admin (reuses `PushService`).
- Seven new templates in the registry, and two new settings.

## Why
W2 ended at "payment received". A booker then heard nothing until the visit, and a caregiver dropping out left a paid booking stranded. The booker also had no way to see or cancel a booking without calling. All of it had to reuse the W2 outbox, the 24-hour window rule and the existing money paths, not fork them.

## How

```
BookingService.handle_booking_action  --(same transaction)-->  booking_events rows  --(worker, every 15 s)-->  WhatsApp template / free text   (booker events)
booking_cancellation.cancel_by_booker                                                                    -->  Expo push                      (caregiver events)
```

| Transition (existing code) | Events recorded | Booker hears (template) |
|---|---|---|
| caregiver accepts | `caregiver_assigned`, `visit_code_issued` (keyed by caregiver) | `caregiver_assigned`, `visit_code` |
| on my way | `caregiver_en_route` | `caregiver_en_route` |
| start (code accepted) | `visit_started` | `visit_started` |
| complete | `visit_completed` | `visit_completed_feedback` |
| caregiver cancels (WhatsApp bookings) | `booking_cancelled_by_caregiver`, plus the refund flag on the booking | `booking_update` |
| booker cancels in chat | `caregiver_booking_cancelled` (caregiver push) | the chat reply itself |
| admin cancel / refund (hooks, see follow-ups) | `NotificationService.cancelled_by_admin / refund_flagged / refund_processed` | `booking_update`, `refund_update` |

### Decisions and tradeoffs
| Decision | Choice | Why / alternatives rejected |
|---|---|---|
| Send inline vs outbox | Outbox rows in the transaction; the W2 worker delivers | A slow or failing Meta/Expo can never roll back or delay a booking transition, and an event exists if and only if the change committed. Rejected: calling Meta from inside `handle_booking_action`. |
| What an event stores | `(booking, event, dedupe_key)` only; the message is built at delivery from the booking as it is *then* | A stale event (caregiver already arrived, replaced, booking cancelled) is dropped instead of announced; no health data and **no visit code** is ever copied into the outbox. |
| Unique key | `(booking_id, event, dedupe_key)`; the key is the caregiver id for assignment events | The W2 key (`booking, event`) would silence the announcement of a *replacement* caregiver. Needs migration 0006. |
| Who is notified | Booker events only for `channel = 'whatsapp'`; caregiver push for **every** booking with an assigned caregiver | App bookings have no WhatsApp booker, so no rows pile up; a caregiver wants to know whichever way the booking came. |
| Visit code | Sent to the booker as the `visit_code` template (free text inside the window); decrypted at delivery from the existing Fernet field; shown again in My bookings to the **booker only** | The existing design (4 digits, Fernet, compare in constant time, 3 wrong tries lock it) is untouched. The code is never logged, never in the outbox, and the message log stores no bodies. Rejected: a second copy of the code in the outbox. |
| Caregiver cancels | WhatsApp bookings: flagged for a **refund** (full if the visit had not started, amount left to a person if it had) and the booker is told. **Not re-dispatched.** | Re-dispatch means reopening a cancelled booking (`PENDING`, no caregiver, new offers), which changes the mobile contract of "cancel" and needs a product call. The booker gets `booking_update` and a **Book again** path. App bookings behave exactly as before. |
| Cancellation fee | `WHATSAPP_CANCEL_FEE_PERCENT` (default **20**) of the **service fee** (PRD D4), rounded half up; applies to an assigned visit closer than `WHATSAPP_CANCEL_FREE_HOURS` (12), or already on the way | The PRD marks the percent as **[DECISION]**; it is a setting so the founder can change it without a deploy. A 0% fee means full refunds. |
| Refund amount | New `bookings.refund_amount_kobo` (NULL = the refund module decides) | The existing `refund_required` flag had no amount, and a fee cannot be expressed without one. |
| Cancel in progress | Refused; opens a `talk_to_person` ticket and hands the chat to a human | WA-FR-39. |
| Who may cancel | The booker (`booked_by_user_id`, or the patient for an app booking). Someone who only *cares for* the recipient can view but is pointed at the team | Cancelling releases money; a relationship with `clinical_access = false` is logistics-only. |
| Stateless "My bookings" | No new conversation state: every button carries `bk:<action>:<booking id>` and every tap re-checks the booking is the contact's | Nothing to expire or get stuck in; a forged id reveals nothing. |
| Essential vs not (STOP) | Essential (still sent after STOP): `visit_code`, `booking_update`, `refund_update` (and W2's payment ones). Non-essential: assigned, on my way, started, completed | Essential = the message is about their money or a paid visit that will not happen as planned (WA-FR-8). |
| Cancel race | `cancel_by_booker` takes the booking row lock and re-reads; the chat step holds the conversation lock, so the order is conversation, then booking (the W2 order) | A caregiver starting at the same instant and a double tap each get one consistent result. A mutation test (lock removed) fails the race test. |

Rejected: a `notification_preferences` table (no requirement yet); a separate push-notification table (the outbox already has claim, retries and a sent marker).

## What's involved
- **Service layer:** `app/services/notification_service.py` (new), `booking_cancellation.py` (new: policy + cancel), `booking_events.py` (audience-aware `emit`, `key`), `booking.py` (`handle_booking_action` records events; caregiver cancel flags the refund).
- **Channel:** `app/channels/whatsapp/booking_notices.py` (new: wording per event, stale rules), `my_bookings.py` (new: list, detail, ask/confirm cancel), `notifications.py` (routes caregiver events to push, journey events to `booking_notices`), `engine.py` (`ListBookings`, `ShowBooking`, `AskCancelBooking`, `ConfirmCancelBooking`, `Snapshot.has_account`), `processor.py`, `templates.py`, `copy.py`, `booking_chat.py` (`cancel_unpaid_after_check`, a public name for W2's step).
- **Models / migration:** `app/db/models/whatsapp_booking.py` (event lists, `dedupe_key`), `booking.py` (`refund_amount_kobo`), `app/alembic/versions/0006_whatsapp_notifications.py`.
- **Settings:** `WHATSAPP_CANCEL_FEE_PERCENT` (default 20), `WHATSAPP_CANCEL_FREE_HOURS` (existing, 12) is now the real policy line.
- **Tests / tooling:** `test_whatsapp_notifications.py`, `test_whatsapp_my_bookings.py`, `test_whatsapp_engine_my_bookings.py`, `whatsapp_notify_helpers.py`; `scripts/whatsapp_local_e2e.py` (`notification_scenarios`).

### The new templates (see the owner list below for what to submit)
| Template | Body (variables) | Essential | Buttons |
|---|---|---|---|
| `caregiver_assigned` | Booking {{1}}: {{2}} will visit on {{3}} at {{4}}. Experience: {{5}}. Reply here with any questions. | no | none yet (View booking is W4) |
| `visit_code` | Your visit code for booking {{1}} is {{2}}. Give it to the professional when they arrive, and to no one else. | **yes** | none |
| `caregiver_en_route` | Booking {{1}}: {{2}} is on the way to you now. | no | none yet (Track is W4) |
| `visit_started` | The visit for booking {{1}} has started. | no | none |
| `visit_completed_feedback` | The visit for booking {{1}} is complete. Thank you for choosing Oakly. | no | Quick reply: Book again |
| `booking_update` | Update on booking {{1}}: {{2}} Reply here if you need help. | **yes** | none |
| `refund_update` | Your refund of {{1}} for booking {{2}} is {{3}}. Reply here if you have questions. | **yes** | none |

The bodies differ from the PRD §14 drafts because Meta refuses a body that starts or ends with a variable. {{2}} in `booking_update` is one of a fixed set of sentences built in code (never text from a person). Utility category for all; see "What to check against real Meta" for the visit code.

## Verification
- Baseline on `aade294`: full suite green (1444 passed, as recorded in the W2 SOP). After: see "Result".
- Tests were written against the behaviour in the plan; the production code was written first this time and the tests then run green on the first pass, so the key ones were mutation-checked:
  - **Cancel race:** removing the booking row lock in `cancel_by_booker` makes the cancel-vs-start race test fail (4 of 4 runs); restored.
  - **Transaction:** an accept that is refused records no event; a successful one records exactly assigned + code, and sends nothing itself.
  - **No health data:** a sentinel condition, instructions and address appear in no message (journey, detail, cancel), no push, and the visit code is in no log line, outbox row or message-log row.
  - **Ownership:** a stranger's booking, a forged id, and a recipient's booking seen by a non-booker reveal nothing and cannot be cancelled.
  - **Policy:** full refund before assignment (even 2 h out); full when assigned and over 12 h; fee inside 12 h or when already on the way; fee and window follow the settings; 0% means full; shown before confirming, repeated after.
  - **Money:** the refund flag, reason and kobo amount on every path; a second Confirm queues one refund and one caregiver push.
  - **Unpaid:** Paystack is asked first; paid-late is settled not cancelled; Paystack unreachable cancels nothing.
  - **Window / STOP:** inside the window free text, outside the template; STOP gets only the essential ones; four workers delivering at once send one copy.
- Migration: `test_migrations.py` (single head, models match, downgrade to base, upgrade again) passes.

### Result
RESULT_PLACEHOLDER

## Operate / roll back
- **See what is pending:** `SELECT event, dedupe_key, attempts, last_error FROM booking_events WHERE delivered_at IS NULL;`. A caregiver event with no device settles with `last_error = 'no_device'` (nothing to retry).
- **Refund queue:** `SELECT booking_code, refund_required_reason, refund_amount_kobo, total_amount_kobo FROM bookings WHERE refund_required;` (reasons: `cancelled_by_booker`, `caregiver_cancelled`, plus W2's `paid_after_*`). `refund_amount_kobo` NULL means a person decides (the visit had started, or an older reason).
- **Change the fee without a deploy:** set `WHATSAPP_CANCEL_FEE_PERCENT` (0 to 100) and restart the API.
- **Roll back:** `alembic downgrade -1` removes the W3 events (nobody can still be waiting for them), the key and `refund_amount_kobo`. Old code simply never emits the new events. Deploy migration, API and worker together.
- **Stop the messages without a deploy:** none per event. Clear `WHATSAPP_ACCESS_TOKEN` to stop all sends (events then retry up to 5 times and stop).

## Owner steps (nothing here has been done)
**Templates to submit to Meta, all English, category Utility** (the five from W2 are in the [W2 SOP](2026-10-06-whatsapp-booking.md#owner-steps-nothing-here-has-been-done); this is the full list of twelve):

| # | Template | From | Notes |
|---|---|---|---|
| 1 | `booking_payment_link` | W2 | URL button base `https://checkout.paystack.com/{{1}}` |
| 2 | `payment_link_expiring` | W2 | **Resubmit** (body changed in the W2 review) |
| 3 | `payment_received` | W2 | |
| 4 | `payment_refund_pending` | W2 | |
| 5 | `booking_cancelled_unpaid` | W2 | Quick reply "Book again", payload `menu:book` |
| 6 | `caregiver_assigned` | W3 | 5 variables |
| 7 | `visit_code` | W3 | See below: Meta may want Authentication |
| 8 | `caregiver_en_route` | W3 | |
| 9 | `visit_started` | W3 | |
| 10 | `visit_completed_feedback` | W3 | Quick reply "Book again", payload `menu:book` |
| 11 | `booking_update` | W3 | |
| 12 | `refund_update` | W3 | |

Decide the cancellation fee (default 20% of the service fee is a placeholder, PRD D4) and update the quote's "Free cancellation until 12 hours before the visit" line if you change the window.

## What to check against real Meta
- **`visit_code` category.** It carries a code. Meta may reclassify a Utility template containing a one-time-style code as Authentication (which has a fixed body and a Copy code button). If it is rejected or reclassified, switch the template to the Authentication category: only `templates.py` and the registry entry change, the wording in-window stays free text.
- A body ending in text after a variable (every W3 body now does) is accepted; none starts or ends with a variable.
- Quick-reply tap on `visit_completed_feedback` arrives as `type: button` with payload `menu:book` (same as W2).
- Expo: the push's `data.type = booking_cancelled` is new; the caregiver app does not handle it yet (it still shows the notification).

## Follow-ups
- **Not built (W3 scope as scoped):** `visit_reminder_24h` / `visit_reminder_1h` (WA-FR-32/33), `matching_delayed` (WA-FR-29), `no_caregiver_found` with Full refund / Pick another time buttons (WA-FR-31). They need time-driven scans like W2's reminder; the outbox and template registry are ready for them.
- **Re-dispatch after a caregiver drops out** (WA-FR-37 offers "automatic re-dispatch or refund"). Today it is refund. Needs a decision and a change to the mobile cancel contract.
- **Rating buttons** on `visit_completed_feedback` (1 to 5), visit notes link, caregiver photo header, rating in `caregiver_assigned`, ETA and tracking link in `caregiver_en_route`, View booking links: need ratings storage and the W4 web companion.
- **Admin hooks:** `NotificationService.cancelled_by_admin`, `refund_flagged` and `refund_processed` exist and are tested, but nothing calls them yet because the admin cancel and refund screens are Admin A2's. **A2 must call them in the same transaction as the change** and set `refund_amount_kobo` when it flags a refund.
- App bookings: a caregiver cancelling still does **not** flag a refund (kept as before). Decide whether it should.
- The caregiver app should handle the `booking_cancelled` push (open the bookings list).
- Reschedule (WA-FR-39a, Phase 2).
- The `booking_payment_link` "30 minutes" is still hard-coded (W2 follow-up).
