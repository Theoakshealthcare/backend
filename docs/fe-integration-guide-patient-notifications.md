# FE integration guide: patient notifications (in-app feed and push)

| | |
|---|---|
| **Backend work package** | P8-BE, branch `feature/patient-notifications`, migration `0021_patient_notifications` |
| **Audience** | `apps/patient`, slice P8 (home, notification list, push registration) |
| **Base URL** | `http://127.0.0.1:8013` for the capture run; every path below starts with `/api/v1` |
| **Source of truth** | Every request body, response body, status code and error string below was **copied from a real response** captured by `scripts/capture_patient_notifications.py` against a running uvicorn process on a real Postgres. The files are in [`docs/fe-captures/patient-notifications/`](fe-captures/patient-notifications/); each example names its file. |

## 0. How to read this guide

- **Examples are generated, not typed.** Every JSON block is rendered from the capture named in the comment above it by `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-patient-notifications.md docs/fe-captures/patient-notifications` (`--check` fails if the guide is stale). Never edit an example by hand: re-capture, then re-render. (CI does not run this check for this guide yet; see the SOP follow-ups.)
- **Re-derive a capture.** Start a scratch Postgres, run `alembic upgrade head`, start the API, then run `poetry run python -m scripts.capture_patient_notifications --base-url <url>` (it needs `DATABASE_URL` and the same secrets as the API in its environment; the docstring has the details). The script rewrites the capture directory.
- **What was real, what was seeded, what was faked.**
  - **Real HTTP calls to the running API:** sign-in, `POST /devices/push-token`, the three feed endpoints, the caregiver's accept / on-my-way / start / complete / cancel (those make the events), and `GET /booking/booking/{id}`.
  - **[seeded]:** the patients, the caregiver and the paid bookings. The patient booking endpoint needs a live Paystack account and caregiver approval needs an admin.
  - **[delivery run in-process]:** after each step the capture process calls the real delivery function (`deliver_pending`, the body of the worker's `whatsapp_events_job`). In production the ARQ worker runs it **every 15 seconds** (`second={0, 15, 30, 45}`), so a notice appears in the feed up to about 15 seconds after the action (the cadence is the configured cron, **not measured live**).
  - **[in-process event]:** events whose trigger cannot be driven over HTTP in the capture (Oakly cancels, refund started / complete, payment received, reassignment, no caregiver found, caregiver arrived) were recorded by calling the real `NotificationService` method (or `booking_events.emit` for payment received) and delivered by the same function. The wording and payloads are real; the trigger is not. Each such capture says `IN-PROCESS` in its `note`.
  - **[fake Expo]:** Expo was a **local fake receiver**. The `*-expo-*` captures show exactly the request bodies the delivery code sent; the ticket responses are the fake's, shaped after Expo's documented format. **No real push was delivered, and none can be without an EAS project, APNs/FCM credentials and a physical device.** Whether a phone shows the banner, plays the sound or opens the right screen on tap is **not verified**.
- Section 8 is the table of what is verified live and what is not.

## 1. What this is, in the app's terms

When something happens to a patient's booking, Oakly writes a **notification** to that patient's feed and, if a phone has registered a push token, sends the same words as a **push**. The feed is the source of truth (a push can be missed or switched off); the push is a nudge to open it.

1. **The feed** is a list on the notifications screen: newest first, each row with a title, a body, a read marker and a deep-link target (the booking).
2. **The badge** is `unread_count` from the feed call.
3. **The push** carries the same title and body, plus `data` for the tap handler.
4. **Only app bookings.** A booking made on WhatsApp keeps being announced on WhatsApp; it never creates feed rows or pushes. A patient who has both gets app notices for the bookings they made in the app only.
5. **Only the booker.** The notices go to the patient who made the booking. Nobody else's feed changes.

### What gets announced

| `type` | When | `title` | `body` | In this capture |
|---|---|---|---|---|
| `booking_confirmed` | payment received | `Booking confirmed` | `Payment received. We're finding a caregiver for your visit.` | in-process event (18) |
| `caregiver_found` | a caregiver accepted | `Caregiver found` | `<first name> will be your caregiver.` | live (05, 06) |
| `caregiver_changed` | Oakly moved the visit to another caregiver | `Caregiver changed` | `<first name> will now be your caregiver.` | in-process event (18) |
| `caregiver_on_the_way` | the caregiver pressed "On my way" | `Caregiver on the way` | `<first name> is on the way.` | live (07, 08) |
| `caregiver_arrived` | the caregiver is at the door | `Your caregiver has arrived` | `Open the app to see your visit code.` | in-process event (16); **triggered since 2026-11-07 by the caregiver's `arrived` action** (below) |
| `visit_started` | the caregiver entered the visit code | `Visit started` | `Your visit has started.` | live (10, 11) |
| `visit_completed` | the caregiver completed the visit | `Visit completed` | `Your visit is complete.` | live (12, 13) |
| `booking_cancelled` | the caregiver cancelled | `Booking cancelled` | `Your caregiver can no longer attend. Open the app for details.` | live (14, 15) |
| `booking_cancelled` | Oakly cancelled | `Booking cancelled` | `Oakly has cancelled this visit. Open the app for details.` | in-process event (17) |
| `no_caregiver_found` | dispatch gave up finding one | `No caregiver found yet` | `We couldn't find a caregiver yet. Our team is looking into it.` | in-process event (19) |
| `refund_started` | a refund was started | `Refund started` | `We've started your refund.` | in-process event (17) |
| `refund_completed` | the refund was sent | `Refund complete` | `Your refund has been sent.` | in-process event (17) |
| `refund_needs_bank_details` | Paystack needs the bank account to pay the refund | `Bank details needed` | `We need your bank details to send your refund. Our team will contact you.` | **not captured** (exercised by a test only) |

`<first name>` is the first word of the caregiver's name. Switch on `type`, never on `title` or `body` (the wording may change).

**Not announced, on purpose:** the visit code (the app shows it on the booking), a patient cancelling their own booking (they already have the answer on screen; the cancel slice owns that screen), an unpaid booking expiring, and the WhatsApp-only drop-out messages.

**`caregiver_arrived` is triggered by the caregiver's "I've arrived" action** (`POST /booking/caregiver/action/{id}` with `{"action": "arrived"}`, new 2026-11-07; see the caregiver guide). It fires **once per assignment** (a second tap changes nothing and tells no one again; a replacement caregiver arriving is announced again), for **app bookings only**. The caregiver may never tap it (it is optional, the visit still starts with the code), so **do not make any screen depend on it arriving**: `arrived_at` on the booking (bookings guide, section 5.2) is the state, the notice is only the nudge. **What was shown live:** the booking writes exactly one `caregiver_arrived` outbox row after two taps (caregiver guide, capture 711). **Not shown live:** its delivery (the worker was not running); the wording, feed row and push payload are the in-process capture 16 below.

## 2. Conventions

| Topic | What is true | Evidence |
|---|---|---|
| Auth | `Authorization: Bearer <access_token>` of a **patient**. | 01, 02 |
| Error shape | FastAPI's `{"detail": ...}`: a **string** for `401`, `403`, `404`; a **list of objects** for `422`. | below |
| Timestamps | ISO 8601 **UTC without a `Z`** (`2026-11-07T14:42:04.045673`). Treat as UTC. | 23 |
| IDs | UUID strings. | 23 |
| `read_at` | **Present and `null`** when unread, never absent. | 23 |
| Paging | `limit` (default 20, 1 to 100) and `offset` (default 0, 0 or more). Not cursors. | 23, 24, 25 |

## 3. Register and forget the device token (existing endpoint)

`POST /api/v1/devices/push-token` already existed for the caregiver app and is **not role-specific**: it works for a patient token unchanged (capture 04). Call it after every sign-in and whenever Expo hands the app a new token. A token that was registered to another account moves to the caller, so a phone that changes hands never receives the previous user's pushes. `DELETE /api/v1/devices/push-token` (body `{"token": ...}`, `204`) on sign-out forgets it; see section 6 of [the caregiver guide](fe-integration-guide-caregiver.md) for its captures. Nothing in the patient flow differs.

<!-- capture: 04-register-push-token.json request.body -->
```json
{
  "token": "ExponentPushToken[placeholder-patient-phone]",
  "platform": "ios"
}
```

<!-- capture: 04-register-push-token.json response -->
```json
{
  "status_code": 200,
  "body": {
    "token": "ExponentPushToken[placeholder-patient-phone]",
    "platform": "ios"
  }
}
```

Expo tokens only (`ExponentPushToken[...]`); anything else is a `422` (see the caregiver guide). Without a registered token the patient still gets every feed row, just no push.

## 4. The feed: `GET /api/v1/patient/notifications`

Query: `limit` (default 20, max 100), `offset` (default 0). Newest first (`created_at` descending, ties by `id`). **`unread_count` covers the whole feed, not the page** (a page of 5 with `unread_count: 13` is correct).

A patient with nothing yet:

<!-- capture: 03-feed-empty.json response -->
```json
{
  "status_code": 200,
  "body": {
    "items": [],
    "total": 0,
    "limit": 20,
    "offset": 0,
    "unread_count": 0
  }
}
```

After a visit's worth of events (page 1 of a 13-row feed, `limit=5`). `data` here is the **feed's** `data`: only `booking_id` and `target`:

<!-- capture: 23-feed-page-1.json response.body -->
```json
{
  "items": [
    {
      "id": "e8fae55a-f2f9-40ea-a258-ae1cb5214bf2",
      "type": "visit_completed",
      "title": "Visit completed",
      "body": "Your visit is complete.",
      "data": {
        "target": "booking_detail",
        "booking_id": "b4831745-9ca5-45ca-8009-6e1c1e8b25a3"
      },
      "read_at": null,
      "created_at": "2026-11-07T14:45:27.465509"
    },
    {
      "id": "b188649c-9308-4438-b025-2248beb4fb6e",
      "type": "no_caregiver_found",
      "title": "No caregiver found yet",
      "body": "We couldn't find a caregiver yet. Our team is looking into it.",
      "data": {
        "target": "booking_detail",
        "booking_id": "ff882ad4-ae92-4fdd-bc02-bba0467ef019"
      },
      "read_at": null,
      "created_at": "2026-11-07T14:45:27.429936"
    },
    {
      "id": "a3830c01-7d39-4d7c-8a4d-3bb69def698f",
      "type": "caregiver_changed",
      "title": "Caregiver changed",
      "body": "Ngozi will now be your caregiver.",
      "data": {
        "target": "booking_detail",
        "booking_id": "b164e965-8bb4-4a07-90d3-d541c17d80ca"
      },
      "read_at": null,
      "created_at": "2026-11-07T14:45:27.407841"
    },
    {
      "id": "75a44dfa-7ace-4d5a-8651-8c3b7ffe9ecc",
      "type": "booking_confirmed",
      "title": "Booking confirmed",
      "body": "Payment received. We're finding a caregiver for your visit.",
      "data": {
        "target": "booking_detail",
        "booking_id": "a297f175-2aec-4054-98f7-3f4724604b3f"
      },
      "read_at": null,
      "created_at": "2026-11-07T14:45:27.392743"
    },
    {
      "id": "2aacb3ff-bc7b-41e2-9ecb-67bd775bd9cd",
      "type": "refund_completed",
      "title": "Refund complete",
      "body": "Your refund has been sent.",
      "data": {
        "target": "booking_detail",
        "booking_id": "7a867cf4-4438-4474-9401-1bb140ec9178"
      },
      "read_at": null,
      "created_at": "2026-11-07T14:45:27.363971"
    }
  ],
  "total": 13,
  "limit": 5,
  "offset": 0,
  "unread_count": 13
}
```

The next page (`offset=5`; its first row is `refund_started`):

<!-- capture: 24-feed-page-2.json response.body keys=total,limit,offset,unread_count -->
```json
{
  "total": 13,
  "limit": 5,
  "offset": 5,
  "unread_count": 13
}
```

The default paging (no query string) is `limit: 20`, `offset: 0`, so this 13-row feed comes back whole. Its last row, the oldest, is the first event of the visit (`caregiver_found`):

<!-- capture: 25-feed-default-paging.json response.body keys=total,limit,offset,unread_count -->
```json
{
  "total": 13,
  "limit": 20,
  "offset": 0,
  "unread_count": 13
}
```

<!-- capture: 25-feed-default-paging.json response.body path=items.12 -->
```json
{
  "id": "8da49e3f-3a15-4469-8091-777368025be4",
  "type": "caregiver_found",
  "title": "Caregiver found",
  "body": "Ngozi will be your caregiver.",
  "data": {
    "target": "booking_detail",
    "booking_id": "f507879c-80e7-4b69-b9d4-d52fb46f7ceb"
  },
  "read_at": null,
  "created_at": "2026-11-07T14:45:27.061924"
}
```

### Item fields

| Field | Type | Meaning |
|---|---|---|
| `id` | string (uuid) | Use it for `POST .../{id}/read` |
| `type` | string | One of the types in section 1. **Open set:** show an unknown `type` with its `title` and `body` and a generic icon |
| `title`, `body` | string | Safe to show on a lock screen and in the list (section 7) |
| `data.booking_id` | string (uuid) | The booking the notice is about |
| `data.target` | string | Where to go on tap. Today always `"booking_detail"` (open that booking). Treat an unknown target as "stay on the list" |
| `read_at` | string or `null` | `null` = unread |
| `created_at` | string | UTC, no `Z` |

### Errors

| Status | `detail` | When | Capture |
|---|---|---|---|
| 401 | `"Not authenticated"` | no `Authorization` header | `01-feed-no-token` |
| 403 | `"Access denied. Patient account required."` | a caregiver (or any non-patient) token | `02-feed-as-caregiver` |
| 422 | list of `{"type","loc","msg","input","ctx"}` | `limit` 0, `limit` over 100, negative `offset` | `26`, `27`, `28` |

<!-- capture: 01-feed-no-token.json response -->
```json
{
  "status_code": 401,
  "body": {
    "detail": "Not authenticated"
  }
}
```

<!-- capture: 02-feed-as-caregiver.json response -->
```json
{
  "status_code": 403,
  "body": {
    "detail": "Access denied. Patient account required."
  }
}
```

<!-- capture: 26-feed-limit-zero.json response -->
```json
{
  "status_code": 422,
  "body": {
    "detail": [
      {
        "type": "greater_than_equal",
        "loc": [
          "query",
          "limit"
        ],
        "msg": "Input should be greater than or equal to 1",
        "input": "0",
        "ctx": {
          "ge": 1
        }
      }
    ]
  }
}
```

`limit=101` and `offset=-1` are `422` the same way (captures 27 and 28; the `loc` and `msg` differ). Clamp in the app instead of relying on the error.

## 5. Mark read

### One: `POST /api/v1/patient/notifications/{id}/read`

No body. Returns the updated item. **Idempotent:** reading an already-read item returns `200` and keeps the **original** `read_at`.

<!-- capture: 29-read-one.json response -->
```json
{
  "status_code": 200,
  "body": {
    "id": "e8fae55a-f2f9-40ea-a258-ae1cb5214bf2",
    "type": "visit_completed",
    "title": "Visit completed",
    "body": "Your visit is complete.",
    "data": {
      "target": "booking_detail",
      "booking_id": "b4831745-9ca5-45ca-8009-6e1c1e8b25a3"
    },
    "read_at": "2026-11-07T14:45:27.526098",
    "created_at": "2026-11-07T14:45:27.465509"
  }
}
```

The same call again (capture 30): `read_at` is unchanged:

<!-- capture: 30-read-one-again.json response.body keys=id,read_at -->
```json
{
  "id": "e8fae55a-f2f9-40ea-a258-ae1cb5214bf2",
  "read_at": "2026-11-07T14:45:27.526098"
}
```

| Status | `detail` | When | Capture |
|---|---|---|---|
| 404 | `"Notification not found."` | the id does not exist **or belongs to someone else** (the two are indistinguishable on purpose) | `31`, `32` |
| 422 | list (`uuid_parsing`) | the id is not a UUID | `33` |
| 401 | `"Not authenticated"` | no token | not captured for this route (same dependency as the feed) |

<!-- capture: 31-read-someone-elses.json response -->
```json
{
  "status_code": 404,
  "body": {
    "detail": "Notification not found."
  }
}
```

<!-- capture: 33-read-bad-id.json response.body -->
```json
{
  "detail": [
    {
      "type": "uuid_parsing",
      "loc": [
        "path",
        "notification_id"
      ],
      "msg": "Input should be a valid UUID, invalid character: found `n` at 1",
      "input": "not-a-uuid",
      "ctx": {
        "error": "invalid character: found `n` at 1"
      }
    }
  ]
}
```

### All: `POST /api/v1/patient/notifications/read-all`

No body. Marks every unread notification of the signed-in patient read. **Idempotent**: a second call marks nothing. `marked_read` is how many it changed, `unread_count` is what is left (always `0` after a success).

<!-- capture: 34-read-all.json response -->
```json
{
  "status_code": 200,
  "body": {
    "marked_read": 12,
    "unread_count": 0
  }
}
```

<!-- capture: 35-read-all-again.json response -->
```json
{
  "status_code": 200,
  "body": {
    "marked_read": 0,
    "unread_count": 0
  }
}
```

<!-- capture: 36-read-all-no-token.json response -->
```json
{
  "status_code": 401,
  "body": {
    "detail": "Not authenticated"
  }
}
```

After it, the feed (page of 2) shows the rows read and the count at zero:

<!-- capture: 37-feed-after-read-all.json response.body keys=total,limit,offset,unread_count -->
```json
{
  "total": 13,
  "limit": 2,
  "offset": 0,
  "unread_count": 0
}
```

Another patient's feed is untouched and empty of this patient's rows:

<!-- capture: 38-feed-patient-b.json response.body -->
```json
{
  "items": [],
  "total": 0,
  "limit": 20,
  "offset": 0,
  "unread_count": 0
}
```

## 6. The push (what Expo delivers to the phone)

The backend POSTs this to Expo for each registered device of the booker. These are the **request bodies the delivery code sent to the fake Expo** (captures `06`, `08`, `11`, `13`, `15`, `16`, `17`, `18`, `19`, `21`). **Nothing here shows a real device receiving anything.**

<!-- capture: 06-expo-caregiver-found.json observed path=observed first=1 -->
```json
[
  {
    "to": "ExponentPushToken[placeholder-patient-phone]",
    "title": "Caregiver found",
    "body": "Ngozi will be your caregiver.",
    "data": {
      "type": "caregiver_found",
      "booking_id": "f507879c-80e7-4b69-b9d4-d52fb46f7ceb",
      "target": "booking_detail",
      "notification_id": "8da49e3f-3a15-4469-8091-777368025be4"
    },
    "sound": "default",
    "priority": "high"
  }
]
```

(The capture file is `{"observed": [<messages>], "note": ...}`; the block shows its `observed` list.)

The arrival push, whose words never include the code (the booking behind it has a visit code; it is not in the message):

<!-- capture: 16-expo-caregiver-arrived.json observed -->
```json
{
  "observed": [
    {
      "to": "ExponentPushToken[placeholder-patient-phone]",
      "title": "Your caregiver has arrived",
      "body": "Open the app to see your visit code.",
      "data": {
        "type": "caregiver_arrived",
        "booking_id": "fba237d5-0001-4d83-94a9-2f10204fc421",
        "target": "booking_detail",
        "notification_id": "5b2cff21-34a9-43f8-a94e-f11be85886d6"
      },
      "sound": "default",
      "priority": "high"
    }
  ]
}
```

### Field-nesting traps

| | Feed item (`GET /patient/notifications`) | Push message `data` |
|---|---|---|
| kind | `type` is a **top-level** field of the item | `type` is **inside `data`** (`data.type`) |
| booking | `data.booking_id` | `data.booking_id` |
| target | `data.target` | `data.target` |
| notification id | `id` (top level) | `data.notification_id` |
| title, body | top level | top level of the Expo message (not in `data`) |

So the tap handler reads `data.type`, `data.booking_id`, `data.target` and `data.notification_id`; the list screen reads `type`, `data.booking_id` and `data.target`. **`data` is not the same object in the two places**: the push has two extra keys (`type`, `notification_id`).

### What the app does with a push

1. **On receipt (foreground):** refetch the feed (or just `unread_count`). Do not insert the push payload into the list yourself: the feed row is already written, and the push has no `created_at` or `read_at`.
2. **On tap:** open the screen named by `data.target` for `data.booking_id` and call `POST .../{data.notification_id}/read` (ignore a `404`).
3. **If the user never grants push permission or the token is rejected:** nothing breaks. The feed is complete without push.
4. **Polling:** refetch the feed when the app comes to the foreground and when the notifications screen opens; while that screen is open, every 30 to 60 seconds is plenty (a notice is written within about 15 seconds of the event, section 0). The home badge can use `limit=1` and read `unread_count`.

### A dead device token is cleaned up

When Expo answers `DeviceNotRegistered` for a token (app uninstalled, token rotated) the backend deletes that token. Capture `21` shows one message per device; capture `22` is the database read after, **not an HTTP response**:

<!-- capture: 22-dead-token-cleanup.json observed -->
```json
{
  "observed": {
    "tokens_before": [
      "ExponentPushToken[placeholder-old-phone]",
      "ExponentPushToken[placeholder-patient-phone]"
    ],
    "tokens_after": [
      "ExponentPushToken[placeholder-patient-phone]"
    ]
  }
}
```

## 7. UX consequences the app must honour

- **Never put the visit code in a notification, and never tell the user it is in one.** The code is on the booking detail (`visit_code`; capture `09` shows it on the booking while the caregiver is en route). The arrival notice says `Open the app to see your visit code.`: the app must make that one tap away (the `data.target` opens the booking, where the code is shown).
- **Lock-screen text is deliberately minimal.** It carries a caregiver's first name and generic wording only: no address, no booking code, no condition, no vitals. Do not enrich the local notification with more.
- **A notice is dropped if it went stale before it was written.** Events are delivered by a worker about every 15 seconds, and each is re-checked against the booking at that moment: "on the way" is not written if the visit has already started, "caregiver found" is not written for a caregiver who was replaced meanwhile. So the feed can legitimately skip a step; do not assume every step has a row, and never derive the booking's status from the feed (use the booking).
- **Cancellation by the patient is not announced.** If the patient cancels in the app, the app shows the result itself; there will be no feed row for it.
- **Refund notices are a courtesy, not the refund state.** Show the refund's real state from the booking once the cancel slice exposes it; the feed only says it started or finished.
- **Unpaid bookings produce nothing.** A cancellation of a booking nothing was paid for is not announced (the patient never had a visit to lose).
- **WhatsApp bookings never appear here.** A patient who booked on WhatsApp and also uses the app sees no notices for that booking in the app.
- **Read state is per notification and permanent.** There is no "unread" call; if the app wants a "mark unread" feature it needs a new endpoint.

## 8. Verification table

| Behaviour | Status | Evidence |
|---|---|---|
| Feed empty state, shape, `unread_count` | **verified live** | captures 03, 23 |
| Paging (`limit`, `offset`, defaults, `total`) | **verified live** | 23, 24, 25 |
| `limit=0`, `limit=101`, negative `offset` are `422` | **verified live** | 26, 27, 28 |
| Newest-first order | **verified live** (13 rows in one run) | 23, 24 |
| `unread_count` is for the whole feed | **verified live** | 23 (5 shown, 13 unread) |
| Read one: `200`, idempotent, `read_at` kept | **verified live** | 29, 30 |
| Read someone else's: `404` | **verified live** | 31 |
| Read unknown id: `404`; bad id: `422` | **verified live** | 32, 33 |
| Read all: counts, idempotent | **verified live** | 34, 35, 38 |
| `401` without a token (feed, read-all) | **verified live** | 01, 36 |
| `401` without a token on `POST .../{id}/read` | not captured | same dependency as the feed |
| Caregiver token gets `403` | **verified live** | 02 |
| Patient registers a push token (existing endpoint) | **verified live** | 04 |
| `caregiver_found`, `caregiver_on_the_way`, `visit_started`, `visit_completed`, `booking_cancelled` (by the caregiver): feed row + push payload | **verified live** (caregiver actions over HTTP, delivery in-process, **fake Expo**) | 05 to 15 |
| `booking_confirmed`, `caregiver_changed`, Oakly cancel, `refund_started`, `refund_completed`, `no_caregiver_found`, `caregiver_arrived`: wording, feed row, push payload | **in-process event** (real service method, real delivery, fake Expo); the **trigger** was not driven over HTTP | 16 to 19 |
| `refund_needs_bank_details` | **tests only** | `test_patient_notifications.py` |
| `caregiver_arrived` is triggered by the backend (the caregiver's `arrived` action; once per assignment) | **Yes for the trigger** (one outbox row after two taps: caregiver guide capture 711); delivery **[not live]** | caregiver guide section 7; `test_caregiver_arrived_earning.py` |
| Visit code never in a push | **verified live** for the arrival push on a booking that has a code; also tests | 16 |
| Dead Expo token is deleted, the other device's token kept | **verified live** (fake Expo answered `DeviceNotRegistered`; read from the database) | 21, 22 |
| A worker delivers within about 15 s | **not measured** (configured cron `second={0, 15, 30, 45}`); the capture called the delivery function directly | `app/worker.py` |
| A real Expo push reaches a device, banner, sound, tap-to-open | **not verified**: needs an EAS project, APNs/FCM credentials and a physical device | section 0 |
| The patient feed for a WhatsApp booking stays empty | **tests only** | `test_patient_notifications.py` |
