# FE integration guide: chat closes after the visit, visit-note windows, location matching and the stale-location nudge

| | |
|---|---|
| **Backend work package** | branch `feature/post-visit-windows-location`, migration `0029_location_nudged_at`; [SOP](sop/2026-11-25-post-visit-windows-location.md) |
| **Audience** | `apps/patient` (chat screen, booking detail) and `apps/caregiver` (Chats list, visit screen, notes, push handling, location reporting) |
| **Base URL** | local test harness; every path starts with `/api/v1` |
| **Source of truth** | Every body below is **rendered from a capture** in [`docs/fe-captures/post-visit-windows-location/`](fe-captures/post-visit-windows-location/) written by `app/tests/test_capture_post_visit_windows.py` (the real app and a real Postgres; **Expo is a fake** that records the request the app makes to it). Data is placeholder. Timestamps are **naive UTC** (no `Z`), as everywhere in this API: read them as UTC. Re-render: `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-post-visit-windows-location.md docs/fe-captures/post-visit-windows-location` (`--check` fails when stale). |

## 1. Chat closes 24 hours after the visit

**Rule.** A booking chat can be written to while the booking is not over, and for **24 hours after the visit was completed** (`CHAT_OPEN_HOURS_AFTER_VISIT`, counted from the visit's recorded end). After that it is **read-only**. A **cancelled or declined** booking is read-only **at once**. The history stays readable by both people, always. The rule is the same for the patient and the caregiver.

| Booking status | `chat_open` | `chat_closes_at` |
|---|---|---|
| `pending`, `accepted`, `en_route`, `in_progress` | `true` | `null` (no fixed end yet) |
| `completed`, less than 24 h after the end | `true` | end + 24 h |
| `completed`, 24 h or more after the end | `false` | end + 24 h (in the past) |
| `cancelled`, `declined` | `false` | `null` (already closed) |

### 1.1 Where the fields are
- **Caregiver app:** every item of `GET /caregiver/chats`.
- **Patient app (and caregiver):** every booking payload built from `BookingResponse`: `GET /booking/booking/{id}`, `GET /booking/patient/bookings`, `GET /booking/caregiver/bookings` and the create response. The caregiver action response (`POST /booking/caregiver/action/{id}`) does **not** carry them. There is no patient chat-list endpoint; the patient chat screen reads the booking.

**Field-nesting trap:** on the chat list the fields sit on each **item**; on the booking payloads they sit at the **top level of the booking**, next to `status` (not inside `patient_details` or `caregiver_details`). They are always present (never absent): `chat_open` is a boolean, `chat_closes_at` is a datetime or `null`.

<!-- capture: 001-c-chats-list-chat-open.json response.body.items first=2 -->
```json
[
  {
    "booking_id": "4eae9476-392c-4bb0-9174-ad138725ea02",
    "patient_name": "Placeholder Ada Okafor",
    "patient_photo_url": null,
    "service_type": "nurse",
    "status": "in_progress",
    "scheduled_date": "2026-11-26T08:06:02.135632",
    "last_message": null,
    "chat_open": true,
    "chat_closes_at": null
  },
  {
    "booking_id": "53ad3ac3-32a4-4a13-83d6-1784601f2d60",
    "patient_name": "Placeholder Ada Okafor",
    "patient_photo_url": null,
    "service_type": "nurse",
    "status": "cancelled",
    "scheduled_date": "2026-11-26T08:06:02.129792",
    "last_message": null,
    "chat_open": false,
    "chat_closes_at": null
  }
]
```

Patient booking detail, completed 2 hours ago (chat still open), only the relevant keys kept:

<!-- capture: 002-p-booking-detail-chat-open.json response.body -->
```json
{
  "id": "44d57682-6409-44f1-b3a6-2ec29379f159",
  "status": "completed",
  "chat_open": true,
  "chat_closes_at": "2026-11-26T06:06:02.106091",
  "notes_open": null,
  "notes_closes_at": null,
  "notes_readable_until": null
}
```

Completed 30 hours ago (closed):

<!-- capture: 003-p-booking-detail-chat-closed.json response.body -->
```json
{
  "id": "5e44e968-b928-481c-ae59-b068ae79a911",
  "status": "completed",
  "chat_open": false,
  "chat_closes_at": "2026-11-25T02:06:02.124507",
  "notes_open": null,
  "notes_closes_at": null,
  "notes_readable_until": null
}
```

Cancelled:

<!-- capture: 004-p-booking-detail-cancelled.json response.body -->
```json
{
  "id": "53ad3ac3-32a4-4a13-83d6-1784601f2d60",
  "status": "cancelled",
  "chat_open": false,
  "chat_closes_at": null,
  "notes_open": null,
  "notes_closes_at": null,
  "notes_readable_until": null
}
```

### 1.2 Sending to a closed chat: `409 chat_closed`
`POST /booking/chat/{booking_id}/send` on a closed chat returns **409** with a stable code. Nothing is saved and **nobody is pushed** for the rejected message. Order of checks: 404 (no booking), 403 (not your booking), then 409 `chat_closed`.

<!-- capture: 005-p-chat-send-closed.json observed -->
```json
{
  "request": {
    "method": "POST",
    "path": "/api/v1/booking/chat/5e44e968-b928-481c-ae59-b068ae79a911/send",
    "auth": "Bearer <ACCESS_TOKEN_JWT>",
    "body": {
      "message": "Placeholder: are you still there?"
    }
  },
  "response": {
    "status_code": 409,
    "body": {
      "detail": {
        "code": "chat_closed",
        "message": "This chat is closed. You can still read the conversation, but you can no longer send messages."
      }
    }
  }
}
```

The caregiver gets the identical body (capture `006`). A cancelled booking returns the same code:

<!-- capture: 007-p-chat-send-cancelled.json response -->
```json
{
  "status_code": 409,
  "body": {
    "detail": {
      "code": "chat_closed",
      "message": "This chat is closed. You can still read the conversation, but you can no longer send messages."
    }
  }
}
```

### 1.3 Reading history still works
`GET /booking/chat/{booking_id}/messages` is unchanged and keeps returning the history after the chat closed:

<!-- capture: 008-p-chat-history-closed.json response -->
```json
{
  "status_code": 200,
  "body": [
    {
      "id": "1f82bb2d-c9e3-44fd-9391-feab3be5ac70",
      "booking_id": "5e44e968-b928-481c-ae59-b068ae79a911",
      "sender_id": "a2f69cfb-e5c3-42eb-9462-5aba69d01b8c",
      "message": "Placeholder: see you next week.",
      "message_type": "text",
      "attachment_url": null,
      "is_read": false,
      "created_at": "2026-11-24T01:06:02.551508"
    }
  ]
}
```

### 1.4 What the app must do
- **Disable the composer** when `chat_open` is `false` and show the history read-only with a plain line such as "This chat is closed." Do not hide the history.
- When `chat_closes_at` is set and in the future, you may show "Chat closes in 5 h". Compare it to the **server's** clock, not the phone's, where you can; a phone with a wrong clock would otherwise disable a live chat or keep a closed one enabled. The server is the authority: a send after the cutoff gets the 409 whatever the app thinks.
- **Handle `409 chat_closed` on send** (the chat can close while the screen is open): show the message from the response, disable the composer and keep the typed text visible. Do not retry.
- **WebSocket:** there is no live chat WebSocket. `app/api/v1/endpoints/websocket.py` is not mounted and does not import (it references a function that does not exist), so chat is REST only (send, poll, push). If a chat WebSocket is built later it must enforce the same rule through `app/services/visit_windows.py`.

## 2. Typed visit notes follow the voice-note windows

**Rule (same as voice notes, one shared helper, `app/services/visit_windows.py`).**

| What | When |
|---|---|
| **Add** a note | while the visit is `in_progress`, and for **2 hours after it was completed** (`VOICE_NOTE_POST_VISIT_GRACE_MINUTES`, 120) |
| **Read / save / delete** the caregiver's own notes | while the visit is live (`accepted`, `en_route`, `in_progress`) and for **24 hours after it ended** (`VOICE_NOTE_EDIT_WINDOW_HOURS`, 24) |
| A caregiver who is **no longer approved** | keeps only a visit that is `in_progress` |
| The **patient** reading the notes | not windowed (any time, as before) |

There are two typed-note surfaces and they differ:

- **Booking notes** (`POST /booking/notes/{booking_id}/create`, `GET /booking/notes/{booking_id}`): these had **no time or status rule at all** before; they now follow the windows above.
- **Care-plan visit notes** (`POST /caregiver/care-plans/{plan_id}/observations`): these were **already stricter and are unchanged**: a note can only be added during a visit that is `in_progress` (`409 visit_not_in_progress` otherwise), notes cannot be edited or deleted, and the caregiver loses access to the plan the moment the booking completes. They do **not** get the 2-hour grace (that needs plan access after completion, a privacy decision for the owner). `notes_open` below describes the booking-notes and voice-note rule; for a care-plan note the stricter rule still applies.

### 2.1 Fields on the caregiver's booking payloads
`notes_open`, `notes_closes_at` and `notes_readable_until` appear on the **assigned caregiver's own** booking payloads (`GET /booking/caregiver/bookings`, and `GET /booking/booking/{id}` when the caller is the assigned caregiver). For everyone else (the patient, an admin, a caregiver who only holds an offer) they are `null`.

| Field | Meaning |
|---|---|
| `notes_open` | `true` if a note can be added **now** (in progress, or inside the 2 h grace) |
| `notes_closes_at` | end of the grace (end + 2 h) once the visit is completed; `null` while the visit is in progress or not started |
| `notes_readable_until` | end + 24 h once completed: after it the caregiver can no longer read, save or delete their notes for that visit; `null` until completed |

<!-- capture: 009-c-bookings-notes-window.json response.body -->
```json
[
  {
    "id": "4eae9476-392c-4bb0-9174-ad138725ea02",
    "status": "in_progress",
    "chat_open": true,
    "chat_closes_at": null,
    "notes_open": true,
    "notes_closes_at": null,
    "notes_readable_until": null
  },
  {
    "id": "19687e3c-17eb-4830-890c-8fd205e3e371",
    "status": "completed",
    "chat_open": true,
    "chat_closes_at": "2026-11-26T07:06:02.118941",
    "notes_open": true,
    "notes_closes_at": "2026-11-25T09:06:02.118941",
    "notes_readable_until": "2026-11-26T07:06:02.118941"
  },
  {
    "id": "44d57682-6409-44f1-b3a6-2ec29379f159",
    "status": "completed",
    "chat_open": true,
    "chat_closes_at": "2026-11-26T06:06:02.106091",
    "notes_open": false,
    "notes_closes_at": "2026-11-25T08:06:02.106091",
    "notes_readable_until": "2026-11-26T06:06:02.106091"
  },
  {
    "id": "5e44e968-b928-481c-ae59-b068ae79a911",
    "status": "completed",
    "chat_open": false,
    "chat_closes_at": "2026-11-25T02:06:02.124507",
    "notes_open": false,
    "notes_closes_at": "2026-11-24T04:06:02.124507",
    "notes_readable_until": "2026-11-25T02:06:02.124507"
  }
]
```

Order above: in progress, completed 1 h ago, completed 2 h ago (grace just over), completed 30 h ago. **Trap:** `notes_open: false` with `notes_closes_at: null` can mean "visit not started yet": check `status`.

### 2.2 Errors
Add a note inside the window (capture `010`, response `200`, the existing note shape). Outside it:

<!-- capture: 011-c-note-create-after-grace.json response -->
```json
{
  "status_code": 409,
  "body": {
    "detail": {
      "code": "visit_not_active",
      "message": "Notes can be added during a visit and for a short time after it ends."
    }
  }
}
```

Reading booking notes as the caregiver after the 24 h (the patient still reads them):

<!-- capture: 012-c-notes-read-after-24h.json response -->
```json
{
  "status_code": 404,
  "body": {
    "detail": {
      "code": "not_found",
      "message": "This note could not be found."
    }
  }
}
```

A caregiver who is no longer approved gets the same `404 not_found` for a completed visit (unit test `test_a_suspended_caregiver_keeps_only_a_visit_in_progress`; not in a capture). These errors are dicts (`detail.code`, `detail.message`), not the plain strings the other booking-notes errors still use (`"Booking not found or access denied"`, `"Access denied"`).

### 2.3 What the app must do
- Hide or disable "Add note" when `notes_open` is `false`; show a plain "Notes can be added during a visit and for 2 hours after it" if the caregiver asks why.
- Handle `409 visit_not_active` (the window can end while the note is being typed): keep the text on screen, tell the caregiver it can no longer be saved, and do not retry.
- After `notes_readable_until` the notes disappear from the caregiver's view (`404 not_found`): do not cache them past that time.

## 3. Matching on the last known location (6 hours) and the location nudge

### 3.1 What changed in matching
An **available, approved, not suspended** caregiver is offered visits on their **last known location for up to 6 hours** (`AVAILABLE_LOCATION_MAX_HOURS`), not 15 minutes. All other rules are unchanged (skill, radius, approval, account active). Among caregivers at the same distance a fix older than 15 minutes ranks below a fresher one. Nothing changes in any request or response: this is server behaviour. The app should still send location pings whenever it is open; a fix older than 6 h means no offers.

The on-my-way distance and live tracking have their own rules and are **not** changed (the on-my-way check uses a 5-minute fix; `LOCATION_FRESHNESS_MINUTES` was only used for matching and now only affects ranking).

### 3.2 The `location_stale` push
When an available, approved caregiver's last fix is older than 6 hours the server pushes them. The app receives, through Expo:

<!-- capture: 013-nudge-push-to-expo.json observed -->
```json
{
  "observed": [
    {
      "to": "ExponentPushToken[placeholder-caregiver-device]",
      "title": "Update your location",
      "body": "We have not seen your location for a while. Open Oakly so we can keep offering you visits near you.",
      "data": {
        "type": "location_stale"
      },
      "sound": "default",
      "priority": "high"
    }
  ]
}
```

| Rule | Value |
|---|---|
| Who | approved, not suspended, active account, marked **available**, last fix older than 6 h, at least one push token |
| Not | while a visit is `en_route` or `in_progress`; never between **21:00 and 07:00 Lagos** (`HOME_TIMEZONE`); at most **once per 24 h** per caregiver |
| Tap | `data.type == "location_stale"`: open the app so it reports a location (and, if the caregiver is still available, keep reporting) |

**The app must:** handle `data.type == "location_stale"` by opening to the home screen and sending a fresh location ping with `is_available` set to the caregiver's **current** value (a ping with no flag leaves availability unchanged; one with `true` switches them on). Do not show an extra in-app alert beyond opening the app.

## 4. Verification table

| Behaviour | Verified |
|---|---|
| `chat_open` / `chat_closes_at` on the caregiver chat list and the patient booking detail (open, closed, cancelled) | Yes, live on the test harness (captures `001`-`004`) |
| `409 chat_closed` for patient and caregiver, nothing saved, no push for the rejected message | Yes (captures `005`-`007`; no-save and no-push asserted in `app/tests/test_chat_and_notes_windows.py`) |
| History readable after the chat closed | Yes (capture `008`) |
| Chat WebSocket enforcement | **[not applicable]** there is no mounted chat WebSocket (see 1.4) |
| `notes_open` / `notes_closes_at` / `notes_readable_until` on the caregiver payloads, `null` for the patient | Yes (capture `009`; patient `null` in `002`-`004`) |
| Typed booking-note windows and error bodies (`409 visit_not_active`, `404 not_found`) | Yes (captures `010`-`012`) |
| Care-plan observations unchanged (in progress only) | Not re-run; unchanged code, existing tests in `app/tests/test_care_plan_observations.py` |
| Matching on a 10 min / 5 h / 7 h old fix | **[not live]** unit tests in `app/tests/test_dispatch.py`; no HTTP surface changed |
| Nudge push body | Yes, the request the app makes to Expo, recorded by a fake Expo (capture `013`); the job called directly with an explicit `now` |
| The nudge ARQ cron firing on schedule, and a real push reaching a phone | **[not live]** the cron registration is asserted in `app/tests/test_worker.py`; no real Expo or device |
| Migration `0029` upgrade, downgrade, upgrade | Yes, on a scratch database (`app/tests/test_location_nudge_migration.py`) |
