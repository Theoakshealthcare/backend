# FE integration guide: chat push, photos in chat lists, and the payment return page

| | |
|---|---|
| **Backend work package** | branch `fix/chat-push-payment-return` (no migration); [SOP](sop/2026-11-25-chat-push-payment-return.md) |
| **Audience** | `apps/patient` and `apps/caregiver` (chat, chats list, booking detail, the in-app browser after paying) |
| **Base URL** | local test harness; every path starts with `/api/v1` |
| **Source of truth** | Every body below is **rendered from a capture** in [`docs/fe-captures/chat-push-payment-return/`](fe-captures/chat-push-payment-return/) written by `app/tests/test_capture_chat_push_payment.py` (the real app and a real Postgres; **Expo is a fake** that records the request the app makes to it; Paystack is not involved). Data is placeholder. Re-render: `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-chat-push-payment-return.md docs/fe-captures/chat-push-payment-return` (`--check` fails when stale). |

## 1. A push for every new chat message

When a message is saved by `POST /booking/chat/{booking_id}/send`, the **other** person in that booking's chat gets a push: caregiver to patient, patient to caregiver. The sender never gets one. It is sent after the response, best effort: a failed push never fails the send, and the app must still poll the chat (the push is a nudge, not the source of truth). Nothing changes in the send request or response.

What the device receives (Expo `title`, `body`, `data`):

| Field | Value |
|---|---|
| `title` | the sender's first name; for a **patient** sender, first name plus last initial (`"Ada O."`), the same privacy rule as before a visit is accepted. `"New message"` if the sender has no name. |
| `body` | the message text on one line, at most **100 characters**; a longer text is cut and ends with `…` |
| `data` | `{"type": "chat_message", "booking_id": "<uuid>"}`: on tap, open that booking's chat. Nothing else is in `data`. |

A recipient with no registered push token, or a deactivated account, gets nothing (no error anywhere). Tokens Expo reports dead are pruned, as for every push.

### 1.1 Caregiver sends, patient is pushed
Request the app made to Expo (as recorded by the fake Expo), and the send response is unchanged (capture `001`):

<!-- capture: 001-c-chat-send-pushes-patient.json observed path=push_sent_to_expo -->
```json
[
  {
    "to": "ExponentPushToken[placeholder-patient-device]",
    "title": "Placeholder",
    "body": "Placeholder: I'm on my way, about ten minutes.",
    "data": {
      "type": "chat_message",
      "booking_id": "af6cf0ed-8ac4-49d9-ae68-64dcddf97550"
    },
    "sound": "default",
    "priority": "high"
  }
]
```

### 1.2 Patient sends a long message, caregiver is pushed
<!-- capture: 002-p-chat-send-pushes-caregiver.json observed path=push_sent_to_expo -->
```json
[
  {
    "to": "ExponentPushToken[placeholder-caregiver-device]",
    "title": "Placeholder O.",
    "body": "Placeholder: please bring the blue folder from the hall table and ring the second bell please bring…",
    "data": {
      "type": "chat_message",
      "booking_id": "af6cf0ed-8ac4-49d9-ae68-64dcddf97550"
    },
    "sound": "default",
    "priority": "high"
  }
]
```

The title is first name and last initial and the body is cut. Both captures show exactly one message, to the other person's device only.

### 1.3 What the app must do
- Register tokens as it already does (`/devices`); nothing new to register.
- On a notification tap with `data.type == "chat_message"`, navigate to the chat for `data.booking_id`. If the chat is already open, refresh it instead of showing a banner.
- **Not covered:** the WebSocket `chat_message` event does not save anything and does not push; only the REST send does.

## 2. Photos in chat lists

### 2.1 Caregiver Chats list: `patient_photo_url` (new)
Each item of `GET /caregiver/chats` has `patient_photo_url`: an absolute URL, or `null` (the key is always present). Capture `003`:

<!-- capture: 003-c-chats-list-patient-photo.json response.body -->
```json
{
  "items": [
    {
      "booking_id": "af6cf0ed-8ac4-49d9-ae68-64dcddf97550",
      "patient_name": "Placeholder Ada Okafor",
      "patient_photo_url": "https://res.cloudinary.com/placeholder/image/upload/v1/patients/placeholder-ada.jpg",
      "service_type": "nurse",
      "status": "accepted",
      "scheduled_date": "2026-11-25T23:26:12.085159",
      "last_message": {
        "text": "Placeholder: please bring the blue folder from the hall table and ring the second bell please bring the blue folder from the hall table and…",
        "sent_at": "2026-11-24T23:26:12.822390",
        "from_me": false
      }
    }
  ],
  "next_before": null,
  "next_before_id": null
}
```

On `null` draw the letter avatar. Trap: the booking payload names the same photo `patient_details.profile_image_url`.

### 2.2 Patient side: the caregiver's photo is already in the booking payloads
There is no patient chat-list endpoint. A patient opens a chat from a booking, and the booking carries the caregiver's photo as **`caregiver_details.profile_image_url`** (absolute URL or `null`; `caregiver_details` itself is `null`/absent until a caregiver is assigned). Capture `004` (`GET /booking/booking/{booking_id}`; trimmed with `keys=`):

<!-- capture: 004-p-booking-detail-caregiver-photo.json response.body keys=id,status,caregiver_details,patient_details -->
```json
{
  "id": "af6cf0ed-8ac4-49d9-ae68-64dcddf97550",
  "status": "accepted",
  "caregiver_details": {
    "caregiver_id": "265ff1dc-7fc9-4db8-a9b1-2da16f66ce42",
    "full_name": "Placeholder Chidi Nwosu",
    "specialization": "nurse",
    "profile_image_url": "https://res.cloudinary.com/placeholder/image/upload/v1/caregivers/placeholder-chidi.jpg",
    "current_latitude": null,
    "current_longitude": null,
    "distance_from_patient": null,
    "estimated_arrival_time": null,
    "last_location_update": null,
    "is_online": false
  },
  "patient_details": {
    "patient_id": "3e1ea7af-6a7b-4f41-934a-8af84f1e8dc0",
    "full_name": "Placeholder Ada Okafor",
    "phone_number": "+2348422133213",
    "profile_image_url": "https://res.cloudinary.com/placeholder/image/upload/v1/patients/placeholder-ada.jpg",
    "emergency_contact_name": null,
    "emergency_contact_phone": null
  }
}
```

The list `GET /booking/patient/bookings` carries the same `caregiver_details` per booking (capture `005`, first item):

<!-- capture: 005-p-booking-list-caregiver-photo.json response.body first=1 -->
```json
[
  {
    "id": "af6cf0ed-8ac4-49d9-ae68-64dcddf97550",
    "booking_code": "B9EE226F",
    "channel": "app",
    "patient_id": "3e1ea7af-6a7b-4f41-934a-8af84f1e8dc0",
    "patient_details": {
      "patient_id": "3e1ea7af-6a7b-4f41-934a-8af84f1e8dc0",
      "full_name": "Placeholder Ada Okafor",
      "phone_number": "+2348422133213",
      "profile_image_url": "https://res.cloudinary.com/placeholder/image/upload/v1/patients/placeholder-ada.jpg",
      "emergency_contact_name": null,
      "emergency_contact_phone": null
    },
    "caregiver_id": "265ff1dc-7fc9-4db8-a9b1-2da16f66ce42",
    "caregiver_details": {
      "caregiver_id": "265ff1dc-7fc9-4db8-a9b1-2da16f66ce42",
      "full_name": "Placeholder Chidi Nwosu",
      "specialization": "nurse",
      "profile_image_url": "https://res.cloudinary.com/placeholder/image/upload/v1/caregivers/placeholder-chidi.jpg",
      "current_latitude": null,
      "current_longitude": null,
      "distance_from_patient": null,
      "estimated_arrival_time": null,
      "last_location_update": null,
      "is_online": false
    },
    "service_type": "nurse",
    "assistance_type": "home_visit",
    "condition": null,
    "duration_minutes": 240,
    "duration_hours": 4,
    "scheduled_date": "2026-11-25T23:26:12.085159",
    "scheduled_end_date": "2026-11-26T03:26:12.085159",
    "patient_address": "1 Test Close, Lekki",
    "patient_latitude": 6.4474,
    "patient_longitude": 3.4723,
    "patient_city": "Lagos",
    "patient_state": "Lagos",
    "status": "accepted",
    "payment_status": "paid",
    "total_amount": 44000.0,
    "service_fee": 40000.0,
    "platform_fee": 4000.0,
    "special_instructions": null,
    "booking_for": "myself",
    "created_at": "2026-11-24T23:26:12.097697",
    "updated_at": "2026-11-24T23:26:12.097703",
    "dispatch_state": null,
    "arrived_at": null,
    "caregiver_earning": null,
    "visit_code": null,
    "cancel_fee_tier": null,
    "cancel_fee": null,
    "refund": null,
    "payment_url": null,
    "payment_access_code": null,
    "payment_error": null
  }
]
```

Field-nesting trap: patient side `caregiver_details.profile_image_url`; caregiver side chat list `patient_photo_url`; caregiver booking detail `patient_details.profile_image_url`.

## 3. The payment return page: `GET /api/v1/payment/callback`

After paying, Paystack redirects the in-app browser to `PAYMENT_CALLBACK_URL` (now this API route; it used to be a website page that answered 404, and the booking-create `callback_url` pointed at another missing page). The page is static HTML: "We're confirming your payment", a note that the person can close it and go back to the app, and a **Return to the app** button linking to **`oaklypatient://payment-complete`**.

- Public, no token. **200 for any query string** (Paystack appends `?trxref=...&reference=...`). The query is never read or printed (capture `007` compares a hostile query with a bare request).
- It **does not mark anything paid**: only the signed Paystack webhook (and the app's own verify call) does. The app must therefore, on returning, poll the booking (`GET /booking/booking/{id}`) until `payment_status` is `paid`, and not treat the redirect as proof of payment.
- Headers (capture `006`; the body is HTML, the full page is in the capture file):

<!-- capture: 006-none-payment-callback.json response.headers -->
```json
{
  "content-security-policy": "default-src 'none'; style-src 'sha256-RGKw5P/KLNRFbBXkE04XwJlAggHxMseW1JPMBuPfZXI='; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "cache-control": "no-store",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  "content-type": "text/html; charset=utf-8"
}
```

<!-- capture: 007-none-payment-callback-other-queries.json observed -->
```json
{
  "observed": {
    "bare_status": 200,
    "hostile_query_status": 200,
    "hostile_query_page_identical_to_bare_page": true,
    "hostile_value_appears_in_page": false
  }
}
```

### 3.1 What the app must do
- If the in-app browser (Custom Tab) can be told to close on the `oaklypatient://payment-complete` link, treat that as "back from paying", then poll the booking as above. If the person taps **Return to the app** the same deep link fires.
- **Operations (required after merge):** set `PAYMENT_CALLBACK_URL` to `<API origin>/api/v1/payment/callback` on staging and production, or leave it empty to derive it from `KYC_WIDGET_PUBLIC_BASE_URL`. Until changed, the old website value keeps returning 404.

## 4. Verification table

| Behaviour | Verified |
|---|---|
| Caregiver and patient sends push the other party only, with the title, body and `data` above | Yes, live on the test harness (captures `001`, `002`); Expo is a fake |
| A real push reaching a phone | **[not live]** no real Expo or device |
| A failed Expo call or dead token never fails the send; deactivated recipient gets nothing; 100-character cut | **[not live]** unit tests in `app/tests/test_chat_push.py` |
| `patient_photo_url` on the caregiver chats list, URL and `null` | Yes (capture `003`; `null` case in `test_caregiver_chats.py`) |
| `caregiver_details.profile_image_url` on patient booking detail and list | Yes (captures `004`, `005`) |
| Callback page: 200, same page for hostile query, no echo, CSP headers | Yes (captures `006`, `007`) |
| A real Paystack redirect landing on the page, the deep link opening the app | **[not live]** |
