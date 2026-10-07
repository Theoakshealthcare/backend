# SOP: caregiver cancel window for WhatsApp bookings, and `channel` on caregiver booking responses

| | |
|---|---|
| **Date** | 2026-10-07 |
| **Branch** | `fix/caregiver-cancel-window` (from `main` at `dff3f5f`, after PR #30) |
| **Migration** | none |
| **Decision** | Owner, 2026-10-07 |
| **Status** | Verified locally (tests, CI checks, a live capture). Not pushed. |

## What shipped
- `POST /booking/caregiver/action/{id}` with `cancel` on a **WhatsApp** booking is refused with `409` `{"detail": "It's too close to the visit to cancel in the app. Please contact support."}` unless the booking is `accepted` **and** more than `dropout_redispatch_min_lead_minutes` (120 by default, admin-editable in `booking_policy`) before `scheduled_date`.
- Refused: 119 minutes out, exactly the lead, `en_route` (however far the start), `in_progress`. Allowed: 121 minutes out (the booking reopens for a replacement, as before).
- `channel` (`app | whatsapp | admin`) is now in `BookingResponse` and the action response's booking, so the caregiver app can tell which rule applies.

## Why
A drop-out close to the visit, or after the caregiver set out, leaves a family with no time for an automatic replacement. The owner wants the caregiver to talk to support in that case, not to tap a button. `channel` was on the model but dropped by `response_model`, so the app had no way to know.

## How
- The check is in `BookingService.handle_booking_action`, `cancel` branch, **under the booking row lock**, after the existing "can no longer be cancelled" check. Time basis is the one in `dropout.on_caregiver_cancel`: naive UTC, whole minutes floored, `minutes <= lead` refuses. So "allowed" always means the drop-out takes the `redispatching` branch (never the family-choice branch).
- The code after the check moved into `BookingService.apply_caregiver_dropout(booking, caregiver_id, offers)` (reopen for a replacement, or cancel as before). It has no time rule, so an admin-initiated or other internal drop-out can still reach the late path (family choice, `awaiting_choice`). Tests use it for that (`internal_dropout` in `app/tests/whatsapp_notify_helpers.py`). Today no caregiver request reaches it.
- App-channel bookings: unchanged. Today a caregiver `cancel` on an app booking from `accepted`, `en_route` or `in_progress` sets `status = cancelled`, `cancel_reason = caregiver_cancelled` (the `reason` in the request body is discarded), records a `caregiver_dropouts` row, and does nothing else: no refund flag, no notification, no re-dispatch.
- Admins still reassign or cancel through the A2 routes; that is how support handles late drop-outs.
- Alternatives rejected: a separate error code (the app reads `detail`, like the other `409`s on this endpoint); checking in the endpoint (the rule needs the lock and the policy, so it lives in the service).

## What's involved
`app/services/booking.py` (`handle_booking_action`, `apply_caregiver_dropout`), `app/schemas/booking.py` (`channel` on `BookingResponse` and `CaregiverActionBooking`), `app/tests/test_caregiver_cancel_window.py` (new), `app/tests/whatsapp_notify_helpers.py`, `test_dropout_redispatch.py`, `test_whatsapp_notifications.py` (late drop-out tests now use `internal_dropout`), `scripts/capture_caregiver_flow.py`, `docs/fe-integration-guide-caregiver.md` and `docs/fe-captures/caregiver/`.

`redact_offer` does not touch `channel`; an offered (not yet assigned) caregiver sees it too. It is not sensitive.

## Verification
| Check | Result |
|---|---|
| New tests failed on the base (`dff3f5f`) before the change | yes (the cancel returned `200`; `channel` absent) |
| Refused at 119 min, at the lead, en route, in progress; allowed at 121 min; policy set to 60 checked at 59 and 61; app booking unaffected close to the visit and in progress; `channel` on `GET /booking/booking/{id}`, `GET /booking/caregiver/bookings`, the action response and the offer view | `app/tests/test_caregiver_cancel_window.py` |
| Live: refused `409` against the Docker stack | capture `a-cancel-whatsapp-too-close` in `docs/fe-captures/caregiver/` |
| Full pytest and the CI steps | see the hand-back |

## Operate / roll back
Revert the commits; no data or migration. Change the lead without a deploy: `PATCH /api/v1/admin/booking-policy` (`dropout_redispatch_min_lead_minutes`).

## Follow-ups
- The caregiver app should hide or soften the cancel button for a WhatsApp booking inside the lead, en route and in progress, and show the `detail` text on `409`.
- A support-contact entry point in the caregiver app (the message says "contact support").
- The `awaiting_choice` family-choice flow is not reachable from any caregiver or admin request now; wire an admin "caregiver dropped out" action to `apply_caregiver_dropout` if support wants the family asked instead of a manual reassign.
