# FE integration guide: patient reschedules a booking

| | |
|---|---|
| **Backend work package** | branch `feature/booking-reschedule`, migration `0034_booking_reschedule` (placeholder number, the lead re-points it at merge); [design](plans/2026-11-25-booking-reschedule.md), [SOP](sop/2026-11-25-booking-reschedule.md) |
| **Audience** | `apps/patient` (booking detail, the reschedule screen, the booking list), `apps/admin` (booking detail, policy settings), `apps/caregiver` (the "visit rescheduled" push) |
| **Base URL** | local test harness; every path starts with `/api/v1` |
| **Source of truth** | Every body below is **rendered from a capture** in [`docs/fe-captures/booking-reschedule/`](fe-captures/booking-reschedule/) written by `app/tests/test_capture_booking_reschedule.py` (the real app and a real Postgres; **Expo is a fake** that records the request the app makes to it; Paystack is not involved, the bookings are created already paid). Data is placeholder (names, ids, booking codes, the push token). Timestamps are **naive UTC** (no `Z`): read them as UTC; send the new time as ISO 8601 (a `Z` or an offset is accepted and converted). Re-render: `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-patient-booking-reschedule.md docs/fe-captures/booking-reschedule` (`--check` fails when stale). |

## 1. What it is, in the patient's terms

A patient can move a **paid** visit to another time **free of charge**, up to **2 times per booking**, as long as the visit is **more than 12 hours away** and the new time is **more than 12 hours from now and at most 28 days ahead**. The three numbers are admin settings (`free_hours`, `reschedules_allowed` and the date range come back in every terms response: **never hard-code 12, 2 or 28**).

What happens behind it: the caregiver who had accepted (if any) is released and told; the booking goes back to "finding a caregiver" for the new time; if nobody accepts, Oakly operations match one by hand. The booking is still `status: "pending"` while that happens, and `dispatch_state` says `dispatching` (being matched) or `unmatched` (waiting for operations), the same fields the app already shows for a new booking.

**Do not offer the option** when `can_reschedule` is false on the booking list, or inside 12 hours (the server refuses it anyway, section 5).

## 2. Endpoints (patient, booker only, Bearer token)

| Method and path | Purpose |
|---|---|
| `GET /booking/{booking_id}/reschedule-terms[?new_scheduled_date=ISO]` | Can it be done now, the limits, the allowed date range, the price at the new time; with a target time it also checks that time. Always 200 for the booker. |
| `POST /booking/{booking_id}/reschedule` | Do it. Body `{new_scheduled_date, request_id?, accepted_total_kobo?}`. |
| `GET /booking/patient/bookings` and `GET /booking/booking/{id}` | Now carry `reschedule_count`, `can_reschedule`, `reschedules_left` (section 4). |

A booking id that does not exist is 404 `booking_not_found`; someone else's booking is 403 `not_your_booking` (both for the GET and the POST).

### 2.1 Terms (preview)

<!-- capture: 001-p-terms.json response.body -->
```json
{
  "booking_id": "6930a4b9-2596-485b-bdb1-caeaa2340db0",
  "booking_code": "1DC0524D",
  "can_reschedule": true,
  "reason": null,
  "message": null,
  "current_scheduled_date": "2026-11-27T10:55:12.170540",
  "free_hours": 12,
  "reschedules_used": 0,
  "reschedules_allowed": 2,
  "reschedules_left": 2,
  "earliest_new_time": "2026-11-26T06:55:12.237034",
  "latest_new_time": "2026-12-23T18:55:12.237034",
  "caregiver_assigned": false,
  "price": {
    "paid_amount": 44000.0,
    "paid_amount_kobo": 4400000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": 0.0,
    "difference_kobo": 0,
    "handling": "none",
    "credit_amount": 0.0,
    "credit_kobo": 0
  },
  "new_time_ok": null,
  "new_time_reason": null,
  "new_time_message": null,
  "as_of": "2026-11-25T18:55:12.237034"
}
```

- `can_reschedule` is the booking-level answer (state, payment, the 12 hour window, reschedules left, and the price rule). When false, `reason` and `message` say why (the codes of section 5) and `price` may be `null`.
- `earliest_new_time` (exclusive) and `latest_new_time` (inclusive) bound the date picker, computed on the server clock at `as_of`.
- `price` is what the visit costs at today's prices. **The booking keeps the price it was paid at** (section 6).

With a target time the answer adds `new_time_ok`:

<!-- capture: 003-p-terms-target-too-far.json response.body -->
```json
{
  "booking_id": "6930a4b9-2596-485b-bdb1-caeaa2340db0",
  "booking_code": "1DC0524D",
  "can_reschedule": true,
  "reason": null,
  "message": null,
  "current_scheduled_date": "2026-11-27T10:55:12.170540",
  "free_hours": 12,
  "reschedules_used": 0,
  "reschedules_allowed": 2,
  "reschedules_left": 2,
  "earliest_new_time": "2026-11-26T06:55:12.249422",
  "latest_new_time": "2026-12-23T18:55:12.249422",
  "caregiver_assigned": false,
  "price": {
    "paid_amount": 44000.0,
    "paid_amount_kobo": 4400000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": 0.0,
    "difference_kobo": 0,
    "handling": "none",
    "credit_amount": 0.0,
    "credit_kobo": 0
  },
  "new_time_ok": false,
  "new_time_reason": "new_time_too_far",
  "new_time_message": "The new time can be at most 28 days from now. Please contact support to book further ahead.",
  "as_of": "2026-11-25T18:55:12.249422"
}
```

**Trap:** `can_reschedule` can be `true` while `new_time_ok` is `false`. Check both before enabling the confirm button. `new_time_reason` is `same_time`, `new_time_too_soon` or `new_time_too_far`.

### 2.2 Reschedule, a booking still looking for a caregiver

<!-- capture: 004-p-reschedule-unassigned.json request.body -->
```json
{
  "new_scheduled_date": "2026-11-28T18:55:11.919579Z",
  "request_id": "11111111-1111-4111-8111-111111111111",
  "accepted_total_kobo": 4400000
}
```

<!-- capture: 004-p-reschedule-unassigned.json response.body -->
```json
{
  "booking_id": "6930a4b9-2596-485b-bdb1-caeaa2340db0",
  "booking_code": "1DC0524D",
  "status": "pending",
  "already_rescheduled": false,
  "previous_scheduled_date": "2026-11-27T10:55:12.170540",
  "scheduled_date": "2026-11-28T18:55:11.919579",
  "scheduled_end_date": "2026-11-28T22:55:11.919579",
  "reschedules_used": 1,
  "reschedules_left": 1,
  "caregiver_released": false,
  "dispatch_state": "dispatching",
  "price": {
    "paid_amount": 44000.0,
    "paid_amount_kobo": 4400000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": 0.0,
    "difference_kobo": 0,
    "handling": "none",
    "credit_amount": 0.0,
    "credit_kobo": 0
  }
}
```

### 2.3 Reschedule, a booking with an accepted caregiver

`caregiver_released` is `true`: show "We are finding you a caregiver for the new time", the old caregiver and visit code no longer apply (the booking detail has no `caregiver_id` and no `visit_code` any more).

<!-- capture: 006-p-reschedule-caregiver-released.json response.body -->
```json
{
  "booking_id": "7fe94933-7b94-4be2-9b4f-b6277fea3374",
  "booking_code": "3EDF7E79",
  "status": "pending",
  "already_rescheduled": false,
  "previous_scheduled_date": "2026-11-27T00:55:12.175531",
  "scheduled_date": "2026-11-29T18:55:11.919579",
  "scheduled_end_date": "2026-11-29T22:55:11.919579",
  "reschedules_used": 1,
  "reschedules_left": 1,
  "caregiver_released": true,
  "dispatch_state": "dispatching",
  "price": {
    "paid_amount": 44000.0,
    "paid_amount_kobo": 4400000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": 0.0,
    "difference_kobo": 0,
    "handling": "none",
    "credit_amount": 0.0,
    "credit_kobo": 0
  }
}
```

### 2.4 Idempotency: send a `request_id`

Generate one UUID **per confirm screen** and send it. Sending the same `request_id` again (a retry after a timeout, a double tap, two devices) returns the first result with **`already_rescheduled: true`** and changes nothing, so it never uses a second free reschedule. Repeating the same target time without a `request_id` is treated the same way. The same `request_id` with a different time is 409 `request_id_reused`.

<!-- capture: 005-p-reschedule-replay.json response.body -->
```json
{
  "booking_id": "6930a4b9-2596-485b-bdb1-caeaa2340db0",
  "booking_code": "1DC0524D",
  "status": "pending",
  "already_rescheduled": true,
  "previous_scheduled_date": "2026-11-27T10:55:12.170540",
  "scheduled_date": "2026-11-28T18:55:11.919579",
  "scheduled_end_date": "2026-11-28T22:55:11.919579",
  "reschedules_used": 1,
  "reschedules_left": 1,
  "caregiver_released": false,
  "dispatch_state": "dispatching",
  "price": {
    "paid_amount": 44000.0,
    "paid_amount_kobo": 4400000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": 0.0,
    "difference_kobo": 0,
    "handling": "none",
    "credit_amount": 0.0,
    "credit_kobo": 0
  }
}
```

Treat `already_rescheduled: true` as success, not as an error.

## 3. Caregiver pushes (for the caregiver app)

The caregiver who is released gets this push (booking code only: never the patient, address or condition) and an in-app notice on the booking:

<!-- capture: 007-expo-push-to-released-caregiver.json observed -->
```json
{
  "observed": [
    {
      "to": "ExponentPushToken[placeholder-caregiver]",
      "title": "Visit rescheduled",
      "body": "Booking 3EDF7E79 was rescheduled and is no longer assigned to you.",
      "data": {
        "type": "booking_rescheduled",
        "booking_id": "7fe94933-7b94-4be2-9b4f-b6277fea3374"
      },
      "sound": "default",
      "priority": "high"
    }
  ]
}
```

The new wave is the existing `job_offer` push, and **`scheduled_at` is the new time**:

<!-- capture: 008-expo-push-job-offer-new-time.json observed first=1 -->
```json
{
  "observed": [
    {
      "to": "ExponentPushToken[placeholder-caregiver]",
      "title": "New visit request",
      "body": "Nurse visit in Lagos, Sat 28 Nov, 19:55",
      "data": {
        "type": "job_offer",
        "booking_id": "6930a4b9-2596-485b-bdb1-caeaa2340db0",
        "area": "Lagos",
        "scheduled_at": "2026-11-28T18:55:11.919579Z"
      },
      "sound": "default",
      "priority": "high"
    },
    {
      "to": "ExponentPushToken[placeholder-caregiver]",
      "title": "New visit request",
      "body": "Nurse visit in Lagos, Sun 29 Nov, 19:55",
      "data": {
        "type": "job_offer",
        "booking_id": "7fe94933-7b94-4be2-9b4f-b6277fea3374",
        "area": "Lagos",
        "scheduled_at": "2026-11-29T18:55:11.919579Z"
      },
      "sound": "default",
      "priority": "high"
    }
  ]
}
```

The caregiver app must handle `data.type == "booking_rescheduled"` (open the bookings list, the booking is no longer theirs). An offer for the old time can no longer be accepted: the accept call answers 403 with the message "This visit was rescheduled. Open it again to see the new time, if it is offered to you." unless they were re-offered the new time (then the offer is live again).

## 4. The booking list: hide the option

<!-- capture: 023-p-bookings-list-fields.json response.body first=5 -->
```json
[
  {
    "id": "3368a64e-b533-4812-9aa5-c08e10d09b74",
    "status": "pending",
    "scheduled_date": "2026-11-27T20:55:12.340923",
    "reschedule_count": 0,
    "can_reschedule": true,
    "reschedules_left": 2,
    "dispatch_state": null
  },
  {
    "id": "343c6556-40d4-45dd-84c4-06ece92e8013",
    "status": "pending",
    "scheduled_date": "2026-11-26T00:55:12.301231",
    "reschedule_count": 0,
    "can_reschedule": false,
    "reschedules_left": 2,
    "dispatch_state": null
  },
  {
    "id": "41e9d554-ad67-4b03-8c6d-4ce7050c56a7",
    "status": "pending",
    "scheduled_date": "2026-11-27T20:55:12.308784",
    "reschedule_count": 0,
    "can_reschedule": true,
    "reschedules_left": 2,
    "dispatch_state": null
  },
  {
    "id": "62ab8149-7682-49c4-96d8-534209080dcb",
    "status": "pending",
    "scheduled_date": "2026-11-27T20:55:12.349670",
    "reschedule_count": 0,
    "can_reschedule": false,
    "reschedules_left": 2,
    "dispatch_state": null
  },
  {
    "id": "6930a4b9-2596-485b-bdb1-caeaa2340db0",
    "status": "pending",
    "scheduled_date": "2026-11-28T18:55:11.919579",
    "reschedule_count": 1,
    "can_reschedule": true,
    "reschedules_left": 1,
    "dispatch_state": "dispatching"
  }
]
```

(Only these fields of each booking are shown here.) `can_reschedule` is false for a booking inside 12 hours, with no reschedules left, unpaid, started, finished or cancelled. It is computed without the price, so it does not catch the price rule: the terms endpoint does. **Field-nesting trap:** the three fields sit at the top level of each booking, are `null` for caregivers and admins, and are always present for the patient.

## 5. Errors the FE must handle (all `detail = {code, message}`)

| Status | `code` | When | What to show |
|---|---|---|---|
| 403 | `not_your_booking` | not the booker | the message |
| 404 | `booking_not_found` | | the message |
| 409 | `reschedule_window_closed` | visit is 12 hours away or less | the message (offers cancel instead) |
| 409 | `reschedule_limit_reached` | the free reschedules are used | the message (contact support) |
| 409 | `booking_unpaid` | not paid yet | pay or make a new booking |
| 409 | `booking_already_cancelled`, `booking_not_reschedulable` | cancelled, on the way, started, completed, refunded | the message |
| 409 | `price_increase_needs_support` | the price now is higher than what was paid (`detail.price`) | the message and a contact-support action |
| 409 | `price_changed` | the price differs from `accepted_total_kobo` (`detail.price`) | re-show the new price and ask again |
| 409 | `service_unavailable` | the service was switched off or its length is no longer allowed | the message |
| 409 | `request_id_reused` | same id, other time | a bug on the client |
| 422 | `same_time`, `new_time_too_soon`, `new_time_too_far` | the picked time | the message; keep the picker open |
| 422 | `request_id_too_long` | more than 64 characters | a bug on the client |
| 422 | (FastAPI validation, `detail` is a list) | a missing or malformed `new_scheduled_date` | a bug on the client |
| 503 | `booking_settings_not_configured` | pricing is not set up | "try again later" |

**Trap:** `detail.price` inside a 409 uses the same numbers as the success `price` object (amounts as numbers, kobo as integers).

<!-- capture: 012-p-reschedule-window-closed.json response.body -->
```json
{
  "detail": {
    "code": "reschedule_window_closed",
    "message": "A visit can only be rescheduled more than 12 hours before it starts. You can cancel instead (the cancellation fee may apply)."
  }
}
```

<!-- capture: 010-p-reschedule-limit-reached.json response.body -->
```json
{
  "detail": {
    "code": "reschedule_limit_reached",
    "message": "This booking has used its 2 free reschedules. Please contact support to move it again."
  }
}
```

<!-- capture: 013-p-reschedule-too-far.json response.body -->
```json
{
  "detail": {
    "code": "new_time_too_far",
    "message": "The new time can be at most 28 days from now. Please contact support to book further ahead."
  }
}
```

<!-- capture: 015-p-reschedule-too-soon.json response.body -->
```json
{
  "detail": {
    "code": "new_time_too_soon",
    "message": "The new time must be more than 12 hours from now."
  }
}
```

<!-- capture: 014-p-reschedule-same-time.json response.body -->
```json
{
  "detail": {
    "code": "same_time",
    "message": "That is the time the visit already has. Please choose a different time."
  }
}
```

<!-- capture: 021-p-reschedule-unpaid.json response.body -->
```json
{
  "detail": {
    "code": "booking_unpaid",
    "message": "Pay for this booking first, or make a new booking."
  }
}
```

<!-- capture: 022-p-reschedule-not-your-booking.json response.body -->
```json
{
  "detail": {
    "code": "not_your_booking",
    "message": "Only the person who made this booking can reschedule it."
  }
}
```

## 6. The price: what the family must be told (UX consequences, as instructions)

The pricing engine has no time-of-day input: the price at the new time is the price at today's settings. It only differs from what was paid if an admin changed pricing (or switched surge pricing on) since. The booking **never changes the amount it was paid at** and **no money moves when rescheduling**.

1. **Same price** (`handling: "none"`): nothing to say.
2. **Lower price** (`handling: "credit_due"`, `credit_amount` greater than 0): the family paid more than the price now. Show **"You paid ₦X; the price now is ₦Y. We will refund the ₦Z difference after your visit."** Do **not** promise a date: Finance refunds it once the visit is completed or cancelled. Send the price the person saw as `accepted_total_kobo` so a change in between is caught (`price_changed`).

<!-- capture: 016-p-terms-lower-price.json response.body keys=can_reschedule,price -->
```json
{
  "can_reschedule": true,
  "price": {
    "paid_amount": 50000.0,
    "paid_amount_kobo": 5000000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": -6000.0,
    "difference_kobo": -600000,
    "handling": "credit_due",
    "credit_amount": 6000.0,
    "credit_kobo": 600000
  }
}
```

<!-- capture: 018-p-reschedule-lower-price-credit.json response.body -->
```json
{
  "booking_id": "e57d6569-4426-4e47-b8e1-1e3d3541f0e1",
  "booking_code": "1CC996A5",
  "status": "pending",
  "already_rescheduled": false,
  "previous_scheduled_date": "2026-11-27T20:55:12.320224",
  "scheduled_date": "2026-11-28T18:55:11.919579",
  "scheduled_end_date": "2026-11-28T22:55:11.919579",
  "reschedules_used": 1,
  "reschedules_left": 1,
  "caregiver_released": false,
  "dispatch_state": "dispatching",
  "price": {
    "paid_amount": 50000.0,
    "paid_amount_kobo": 5000000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": -6000.0,
    "difference_kobo": -600000,
    "handling": "credit_due",
    "credit_amount": 6000.0,
    "credit_kobo": 600000
  }
}
```

3. **Higher price**: the app **cannot** move the visit without charging the family, so the server refuses. The terms endpoint already says so (`can_reschedule: false`, `reason: "price_increase_needs_support"`), so **disable the option and show a contact-support action** with the message instead of letting them pick a time. This is the PDF's "manually adjust at the client's request" path.

<!-- capture: 019-p-terms-higher-price.json response.body keys=can_reschedule,reason,message,price -->
```json
{
  "can_reschedule": false,
  "reason": "price_increase_needs_support",
  "message": "The price for this visit is now higher than what you paid, so we cannot move it in the app without charging you. Please contact support and we will arrange it with you.",
  "price": {
    "paid_amount": 33000.0,
    "paid_amount_kobo": 3300000,
    "new_amount": 44000.0,
    "new_amount_kobo": 4400000,
    "difference_amount": 11000.0,
    "difference_kobo": 1100000,
    "handling": "none",
    "credit_amount": 0.0,
    "credit_kobo": 0
  }
}
```

<!-- capture: 020-p-reschedule-price-increase-refused.json response.body -->
```json
{
  "detail": {
    "code": "price_increase_needs_support",
    "message": "The price for this visit is now higher than what you paid, so we cannot move it in the app without charging you. Please contact support and we will arrange it with you.",
    "price": {
      "paid_amount": 33000.0,
      "paid_amount_kobo": 3300000,
      "new_amount": 44000.0,
      "new_amount_kobo": 4400000,
      "difference_amount": 11000.0,
      "difference_kobo": 1100000,
      "handling": "none",
      "credit_amount": 0.0,
      "credit_kobo": 0
    }
  }
}
```

4. **Other things the family will notice and must be told before they confirm:** the assigned caregiver is released and they will be matched again (they may get a different person and will get a **new visit code**); the visit then shows "finding a caregiver"; if nobody accepts it waits for Oakly operations. After a reschedule the cancellation fee tiers count from the **new** time, and a reschedule uses one of the free ones.

<!-- capture: 017-p-reschedule-price-changed.json response.body -->
```json
{
  "detail": {
    "code": "price_changed",
    "message": "The price changed since you last looked. Please check it again.",
    "price": {
      "paid_amount": 50000.0,
      "paid_amount_kobo": 5000000,
      "new_amount": 44000.0,
      "new_amount_kobo": 4400000,
      "difference_amount": -6000.0,
      "difference_kobo": -600000,
      "handling": "credit_due",
      "credit_amount": 6000.0,
      "credit_kobo": 600000
    }
  }
}
```

## 7. Admin console

`GET /admin/bookings/{id}` gains `reschedule_count` and `reschedules` (oldest first). `price_handling: "credit_due"` rows are what Finance owes the family: raise the refund with the existing staff refund once the visit is completed or cancelled. Nothing is queued automatically.

<!-- capture: 024-a-booking-detail-reschedules.json response.body -->
```json
{
  "scheduled_date": "2026-11-28T18:55:11.919579",
  "reschedule_count": 1,
  "reschedules": [
    {
      "created_at": "2026-11-25T18:55:12.331937",
      "old_scheduled_date": "2026-11-27T20:55:12.320224",
      "new_scheduled_date": "2026-11-28T18:55:11.919579",
      "old_total_kobo": 5000000,
      "new_total_kobo": 4400000,
      "price_difference_kobo": -600000,
      "price_handling": "credit_due",
      "caregiver_released": false
    }
  ]
}
```

`GET/PATCH /admin/booking-policy` carry the three settings (`reschedule_free_hours` 0 to 168, `reschedule_max_free` 0 to 20, `reschedule_max_days_ahead` 1 to 365; out of range is 422). A change applies at once to every booking, there is no per-booking snapshot (unlike the cancel tiers).

<!-- capture: 025-a-policy-patch.json response.body -->
```json
{
  "reschedule_free_hours": 12,
  "reschedule_max_free": 2,
  "reschedule_max_days_ahead": 28
}
```

## 8. Verification

| Behaviour | Verified |
|---|---|
| Terms and reschedule bodies, replay, caregiver release, errors 403/409/422, list fields, admin detail, policy PATCH | Yes, live on the test harness (captures `001` to `025`) |
| The release push and the re-offer push | Yes, as the exact request to a fake Expo (captures `007`, `008`). A real device was **not** tested |
| Exactly 12 hours and exactly 28 days, the limit of 2, double submit, reschedule racing a caregiver accept | Yes, by backend tests (`app/tests/test_booking_reschedule.py`), not by captures |
| 503 `booking_settings_not_configured`, `service_unavailable`, `request_id_reused`, `request_id_too_long`, `booking_already_cancelled`, `booking_not_reschedulable` | Yes in backend tests; **no capture**, the shapes follow the same `{code, message}` as the captured ones |
| A real Paystack payment before rescheduling | No: bookings are created already paid. Settlement is covered by its own guide |
| The "nobody accepts, then the admin unmatched queue" path end to end with a worker | Partly: the test calls the dispatch tick with an explicit clock; the ARQ cron schedule is not exercised |
| Patient app and admin console screens | **Not built yet** (next slice) |
