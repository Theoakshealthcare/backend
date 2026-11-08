# SOP: the caregiver's "I've arrived" has a time window (from 60 minutes before the visit)

| | |
|---|---|
| **What shipped** | `POST /booking/caregiver/action/{id}` with `{"action": "arrived"}` is refused earlier than `ARRIVAL_EARLIEST_MINUTES_BEFORE` (60) minutes before the booking's `scheduled_date`: `422` with `detail = {code: "arrived_too_early", message, earliest_at}`. |
| **Branch / commit** | `fix/arrived-errors-admin-cancel-terms` (a separate commit on top of the arrived error codes). Not pushed; the lead pushes. |
| **Owner decision** | 2026-11-08, "Yes, 60 minutes". |
| **Migration** | None (Alembic head stays `0025`; `0026` is reserved). |

## Why
A caregiver could tap "I've arrived" days early and the family's app told them the caregiver was at the door. `on_my_way` already has a window; `arrived` had none.

## How (decisions)
| Decision | Why |
|---|---|
| A Settings value `ARRIVAL_EARLIEST_MINUTES_BEFORE: int = 60` (`app/core/config.py`, `.env.example`), not a policy column | The owner asked for a setting and no migration. It is a deploy-time value; moving it into the admin booking policy later needs a migration and an admin field |
| Allowed at exactly the boundary (`now >= scheduled_date - 60 min`) and at any time after the start | Owner rule: "from 60 minutes before ... any time after, until the visit starts or is completed per the existing status rules" |
| The check runs only when `arrived_at` is still empty | A repeat tap stays the `200` no-op it was, whatever the clock says; the family is not re-told |
| Wrong state wins over too early: a booking that cannot be marked arrived is `409 booking_not_arrivable` even far before the visit | The state error is the more useful one and was already the contract |
| `handle_booking_action(..., now=None)` takes an injectable clock | The tests assert exact instants (60 and 61 minutes) with a fixed far-future booking; no wall clock and no timezone (the Mac is Lagos, CI is UTC) |
| `earliest_at` is the naive UTC instant in ISO form, like every other timestamp in this API | Consistent with `scheduled_date`; the app appends `Z` to parse |
| The message interpolates the setting ("... from 60 minutes before the visit starts.") | The number is never stale if the setting changes |

## What's involved
| Path | Change |
|---|---|
| `app/core/config.py`, `.env.example` | `ARRIVAL_EARLIEST_MINUTES_BEFORE` |
| `app/services/booking.py` | `BookingArrivalTooEarly`; the check in the `arrived` branch; `now` parameter |
| `app/api/v1/endpoints/booking.py` | maps it to the 422 object |
| `app/tests/test_caregiver_arrived_earning.py` | window tests; the existing arrived tests now use a visit 30 minutes away (`soon()`), because the default test booking starts a day out |
| `scripts/capture_arrival_window.py` (new), `docs/fe-captures/caregiver/810-812`, `docs/fe-integration-guide-caregiver.md` | live capture and guide text |

## Verification
| Check | Result |
|---|---|
| Unit tests | Exactly 60 minutes before: allowed; 61: refused with the exact message and `earliest_at`, nothing written, no outbox row; 25 minutes after the start: allowed; a repeat tap asked at 300 minutes before: unchanged, one outbox row; the setting set to 30 is honoured (45 refused, 30 allowed); HTTP shape; a wrong state far out is still 409. With the other arrival, notification and WhatsApp-notification tests: 113 passed |
| Live | Captures `810` (422 for a visit three hours away), `811` (booking unchanged), `812` (200 for a visit 30 minutes away) on a scratch Postgres + Redis, no worker. The exact boundary is **not** live (unit tests with an injected clock) |

## Operate / roll back
Set `ARRIVAL_EARLIEST_MINUTES_BEFORE` (restart the API) to move the window; a very large value (e.g. 100000) effectively disables it. Reverting the commit removes the rule. **Ship the caregiver app change first or together**: an app that does not know the `422` shows a generic error on an early tap.

## Follow-ups
1. The caregiver app should disable or explain the button before the window opens (branch `fix/caregiver-arrived-error-codes` reads the error; a disabled state from `scheduled_date` is a possible next step).
2. If Operations need to tune it, move the value into the admin booking policy (a migration and a console field).
