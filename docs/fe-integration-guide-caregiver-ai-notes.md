# FE integration guide: caregiver AI voice notes

| | |
|---|---|
| **Audience** | `apps/caregiver` (the note screen: consent, record, transcribing, review, save); `apps/patient` and `apps/admin` later (read-only, see 3.8 and 3.9) |
| **Base URL** | every path starts with `/api/v1` |
| **Design** | `docs/plans/2026-11-07-ai-note-transcription.md` (the decisions and the reasons). This guide is what the routes **actually answered**. |
| **Source of truth** | Every body, status code and error string below was **copied from a real response** captured by `app/tests/test_capture_voice_notes.py` (files in `docs/fe-captures/caregiver-ai-notes/`), through the real FastAPI app over a real Postgres. Examples are re-rendered from those files by `scripts/render_fe_guide.py`; CI fails if one drifts. |
| **What was NOT real in the capture** | Three stand-ins, so **no real Gemini call is in these captures**: the AI provider is `FakeGemini` (scripted placeholder text), the audio store is in memory instead of Redis, and the queue only records the job (the test then runs the worker's processing function). The real ARQ + Redis path is exercised by `app/tests/test_voice_notes_redis.py`, not by these captures. **Whether the real Gemini accepts the phone's m4a (`audio/mp4`), how long it takes, and how good the Pidgin transcript is: unverified.** |
| **Placeholders** | All names and text are placeholders. Ids and timestamps are real values from a throwaway database. |
| **Switch** | The feature is off by default on the server (`VOICE_NOTES_ENABLED=false`): until it is on, create and send answer `503 transcription_unavailable` (3.1, section 4). Keep the app's own flag off until the backend is deployed and switched on. |

## 1. What it is, in the app's terms

During a visit the caregiver ticks the consent line, records a short note (2 seconds to 10 minutes, English or Nigerian Pidgin) and the app sends it. Oakly turns it into a **transcript and a short summary** (an AI draft). The caregiver checks and corrects both, then taps **Save**. Only a saved note is seen by anyone else (the booker, and admins with clinical access). **Discard** deletes it on the server. The audio is deleted by the server the moment the text exists; the app deletes its own copy at the same moment.

The old typed note (`/booking/notes/...`) is untouched and separate.

Flow: `POST create` (stores the consent) -> `PUT audio` -> poll `GET` until `ready` or `failed` -> edit -> `PUT save` (or `DELETE`).

## 2. States

| `status` | Meaning | What the app shows | Next calls |
|---|---|---|---|
| `awaiting_audio` | note made, audio not sent (or not accepted) | "Sending..." / unsent card | `PUT audio`, `DELETE` |
| `processing` | audio accepted, being transcribed | "Turning your recording into text..." | poll `GET`, `DELETE` |
| `ready` | an AI draft exists (`transcript`, maybe `summary`) | the review screen, **"AI draft: check before saving"** | `PUT save`, `DELETE` |
| `failed` | see `error.code` and `error.retryable` | the sentence for the code | retryable: `POST retry`, else `PUT audio` again or type instead; `DELETE` |
| `saved` | final text, read-only | the saved note | `DELETE` (only inside the caregiver's window, 3.7) |

`awaiting_audio` notes nobody sends are deleted after 1 hour; `ready` and `failed` drafts after 24 hours (`expires_at` says when); a `processing` note nobody finishes becomes `failed` / `transcription_timeout` after 5 minutes.

## 3. Endpoints

All need the caregiver's bearer token (the booker's for 3.8, an admin's for 3.9). Every success carries `Cache-Control: no-store`.

### 3.1 Create a note (records the consent): `POST /caregiver/visits/{booking_id}/voice-notes`
Body: `consent_confirmed` (**the JSON literal `true`**; `false` is a 400, and any other value such as `"true"` or `1` is a 422), `consent_version` (the version of the consent copy the app showed: `2026-11-v1`), `language` (`en` or `pcm`), `duration_seconds` (2 to 600, the recorder's length), `client_request_id` (a UUID the app makes **once per recording**; optional but always send it).

<!-- capture: 01-create-201.json request.body -->
```json
{
  "consent_confirmed": true,
  "consent_version": "2026-11-v1",
  "language": "pcm",
  "duration_seconds": 42,
  "client_request_id": "11111111-2222-4333-8444-555555555555"
}
```

`201` with the note:

<!-- capture: 01-create-201.json response.body -->
```json
{
  "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
  "booking_id": "1f11e2a9-74a9-4549-b376-7fdea2b9906b",
  "status": "awaiting_audio",
  "language": "pcm",
  "transcript": null,
  "summary": null,
  "summary_available": false,
  "edited": false,
  "error": null,
  "duration_seconds": 42,
  "consent_version": "2026-11-v1",
  "consent_confirmed_at": "2026-11-07T23:25:56.724137",
  "created_at": "2026-11-07T23:25:56.724137",
  "ready_at": null,
  "saved_at": null,
  "expires_at": null
}
```

**Idempotent:** the same `client_request_id` for the same visit answers **`200`** with the **same note**, so a double tap or a retry after a timeout never makes two notes:

<!-- capture: 02-create-repeat-200.json response.status_code -->
```json
200
```

Refusals (all with the `detail.code` shape, see section 4):

| Capture | Status | When |
|---|---|---|
| `03-consent-false-400.json` | 400 | `consent_confirmed` is `false` |
| `04-consent-version-400.json` | 400 | a `consent_version` the server does not know (an old app build) |
| `05-create-missing-field-422.json` | 422 | a required field is missing (**no `detail.code`: a list**) |
| `06-create-duration-422.json` | 422 | `duration_seconds` under 2 or over 600 |
| `07-create-visit-not-active-409.json` | 409 | the visit is yours but not in progress (and not completed within the last 120 minutes) |
| `08-create-not-found-404.json` | 404 | not your visit, no such visit, or the client's account is inactive: **one answer for all of them** |

<!-- capture: 03-consent-false-400.json response -->
```json
{
  "status_code": 400,
  "headers": {
    "cache-control": "no-store",
    "content-type": "application/json"
  },
  "body": {
    "detail": {
      "code": "consent_required",
      "message": "Confirm the client agreed before recording."
    }
  }
}
```

<!-- capture: 05-create-missing-field-422.json response -->
```json
{
  "status_code": 422,
  "headers": {
    "content-type": "application/json"
  },
  "body": {
    "detail": [
      {
        "type": "missing",
        "loc": [
          "body",
          "consent_confirmed"
        ],
        "msg": "Field required"
      }
    ]
  }
}
```

<!-- capture: 07-create-visit-not-active-409.json response.body -->
```json
{
  "detail": {
    "code": "visit_not_active",
    "message": "Voice notes can be added during a visit."
  }
}
```

<!-- capture: 08-create-not-found-404.json response.body -->
```json
{
  "detail": {
    "code": "not_found",
    "message": "This note could not be found."
  }
}
```

### 3.2 Send the audio: `PUT /caregiver/voice-notes/{note_id}/audio`
**The recording itself as the raw request body. Not multipart, not JSON.** `Content-Type` is the audio type: `audio/mp4`, `audio/x-m4a`, `audio/aac`, `audio/mpeg` or `audio/wav`. Up to **12 MiB**. The server also checks that the first bytes look like that container. Allowed from `awaiting_audio` and from `failed`.

`202` with the note now `processing` (request body in the capture is a placeholder for the bytes):

<!-- capture: 15-audio-202.json request -->
```json
{
  "method": "PUT",
  "path": "/api/v1/caregiver/voice-notes/25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5/audio",
  "auth": "Bearer <ACCESS_TOKEN_JWT>",
  "body": "<107 bytes of raw audio, Content-Type audio/mp4>"
}
```

<!-- capture: 15-audio-202.json response.body -->
```json
{
  "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
  "booking_id": "1f11e2a9-74a9-4549-b376-7fdea2b9906b",
  "status": "processing",
  "language": "pcm",
  "transcript": null,
  "summary": null,
  "summary_available": false,
  "edited": false,
  "error": null,
  "duration_seconds": 42,
  "consent_version": "2026-11-v1",
  "consent_confirmed_at": "2026-11-07T23:25:56.724137",
  "created_at": "2026-11-07T23:25:56.724137",
  "ready_at": null,
  "saved_at": null,
  "expires_at": null
}
```

| Capture | Status | When |
|---|---|---|
| `11-audio-empty-400.json` | 400 | zero bytes |
| `12-audio-type-415.json` | 415 | `Content-Type` not in the list |
| `13-audio-not-audio-415.json` | 415 | the type is allowed but the bytes are not that kind of file |
| `14-audio-too-large-413.json` | 413 | over the size cap (captured with the cap lowered; the real one is 12 MiB) |
| `16-audio-again-409.json` | 409 | a second send while one is being processed |
| `37-audio-daily-limit-429.json` | 429 | the caregiver's daily limit (30 uploads **and retries** a day; has `Retry-After`). A retry (3.5) can answer this too |

<!-- capture: 14-audio-too-large-413.json response.body -->
```json
{
  "detail": {
    "code": "audio_too_large",
    "message": "That recording is too long. Keep notes under 10 minutes."
  }
}
```

<!-- capture: 16-audio-again-409.json response.body -->
```json
{
  "detail": {
    "code": "already_processing",
    "message": "This recording is already being processed."
  }
}
```

<!-- capture: 37-audio-daily-limit-429.json response -->
```json
{
  "status_code": 429,
  "headers": {
    "cache-control": "no-store",
    "retry-after": "2041",
    "content-type": "application/json"
  },
  "body": {
    "detail": {
      "code": "daily_limit_reached",
      "message": "You have reached today's limit of voice notes. Please type your note."
    }
  }
}
```

### 3.3 Poll: `GET /caregiver/voice-notes/{note_id}`
Returns one of the caller's own notes. While being transcribed:

<!-- capture: 17-poll-processing.json response.body keys=id,status,transcript,summary,error -->
```json
{
  "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
  "status": "processing",
  "transcript": null,
  "summary": null,
  "error": null
}
```

Done, an AI draft (**`transcript` and `summary` are present only when `status` is `ready` or `saved`**):

<!-- capture: 18-poll-ready.json response.body -->
```json
{
  "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
  "booking_id": "1f11e2a9-74a9-4549-b376-7fdea2b9906b",
  "status": "ready",
  "language": "pcm",
  "transcript": "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was one twenty over eighty. She said she slept well.",
  "summary": "Placeholder client ate breakfast, took the morning tablets, blood pressure 120/80, slept well.",
  "summary_available": true,
  "edited": false,
  "error": null,
  "duration_seconds": 42,
  "consent_version": "2026-11-v1",
  "consent_confirmed_at": "2026-11-07T23:25:56.724137",
  "created_at": "2026-11-07T23:25:56.724137",
  "ready_at": "2026-11-07T23:25:56.995584",
  "saved_at": null,
  "expires_at": "2026-11-08T23:25:56.995584"
}
```

The summary step can fail on its own. The note is still `ready`; **`summary` is `null` and `summary_available` is `false`**. Show the transcript, tell the caregiver "We couldn't write a summary" and let them write one or leave it empty:

<!-- capture: 30-poll-ready-without-summary.json response.body keys=status,transcript,summary,summary_available -->
```json
{
  "status": "ready",
  "transcript": "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was one twenty over eighty. She said she slept well.",
  "summary": null,
  "summary_available": false
}
```

A failure arrives as a **`200`** poll with `status: "failed"` and an `error` object:

<!-- capture: 24-poll-failed-no-speech.json response.body keys=status,transcript,error,expires_at -->
```json
{
  "status": "failed",
  "transcript": null,
  "error": {
    "code": "no_speech_detected",
    "message": "We couldn't hear any speech. Record again, or type your note.",
    "retryable": false
  },
  "expires_at": "2026-11-08T23:25:57.089779"
}
```

<!-- capture: 26-poll-failed-retryable.json response.body keys=status,error -->
```json
{
  "status": "failed",
  "error": {
    "code": "provider_unavailable",
    "message": "The voice service is busy. Try again in a moment.",
    "retryable": true
  }
}
```

### 3.4 List a visit's notes: `GET /caregiver/visits/{booking_id}/voice-notes`
The caller's own notes for the visit, **newest first**, any status. Use it to resume after the app restarts. `{"items": [...]}` with the same note object as above.

<!-- capture: 19-list-200.json response.body path=items first=1 -->
```json
[
  {
    "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
    "booking_id": "1f11e2a9-74a9-4549-b376-7fdea2b9906b",
    "status": "ready",
    "language": "pcm",
    "transcript": "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was one twenty over eighty. She said she slept well.",
    "summary": "Placeholder client ate breakfast, took the morning tablets, blood pressure 120/80, slept well.",
    "summary_available": true,
    "edited": false,
    "error": null,
    "duration_seconds": 42,
    "consent_version": "2026-11-v1",
    "consent_confirmed_at": "2026-11-07T23:25:56.724137",
    "created_at": "2026-11-07T23:25:56.724137",
    "ready_at": "2026-11-07T23:25:56.995584",
    "saved_at": null,
    "expires_at": "2026-11-08T23:25:56.995584"
  }
]
```

### 3.5 Retry: `POST /caregiver/voice-notes/{note_id}/retry`
No body. Only for a `failed` note whose `error.retryable` is `true` **and** whose audio the server still holds (about 30 minutes). Otherwise `409`. If the audio is gone (`audio_expired`), send the recording again with 3.2 (the app keeps its local file until the note is `ready` for exactly this).

<!-- capture: 27-retry-202.json response.body keys=status,error -->
```json
{
  "status": "processing",
  "error": null
}
```

<!-- capture: 29-retry-audio-expired-409.json response.body -->
```json
{
  "detail": {
    "code": "audio_expired",
    "message": "The recording is no longer stored. Please send it again."
  }
}
```

<!-- capture: 25-retry-not-retryable-409.json response.body -->
```json
{
  "detail": {
    "code": "invalid_state",
    "message": "This note changed. Please refresh."
  }
}
```

### 3.6 Save the caregiver's version: `PUT /caregiver/voice-notes/{note_id}`
Body: `transcript` (1 to 20,000 characters after trimming) and `summary` (0 to 2,000; may be empty). Only from `ready`. The server trims both.

<!-- capture: 20-save-200.json request.body -->
```json
{
  "transcript": "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was 120/80. She said she slept well.",
  "summary": "Placeholder client ate breakfast, took the morning tablets, blood pressure 120/80, slept well."
}
```

`200` with the note `saved` (`edited` is `true` because the text differs from the AI draft; `expires_at` is `null`):

<!-- capture: 20-save-200.json response.body -->
```json
{
  "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
  "booking_id": "1f11e2a9-74a9-4549-b376-7fdea2b9906b",
  "status": "saved",
  "language": "pcm",
  "transcript": "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was 120/80. She said she slept well.",
  "summary": "Placeholder client ate breakfast, took the morning tablets, blood pressure 120/80, slept well.",
  "summary_available": true,
  "edited": true,
  "error": null,
  "duration_seconds": 42,
  "consent_version": "2026-11-v1",
  "consent_confirmed_at": "2026-11-07T23:25:56.724137",
  "created_at": "2026-11-07T23:25:56.724137",
  "ready_at": "2026-11-07T23:25:56.995584",
  "saved_at": "2026-11-07T23:25:57.025029",
  "expires_at": null
}
```

Sending the **same text again** is a `200` with the same note (safe after a lost response). **Different text** to a saved note is `409 already_saved`. A save before the note is `ready` is `409 not_ready`. A blank transcript is a `422`.

<!-- capture: 22-save-different-409.json response.body -->
```json
{
  "detail": {
    "code": "already_saved",
    "message": "This note is already saved."
  }
}
```

<!-- capture: 10-save-not-ready-409.json response.body -->
```json
{
  "detail": {
    "code": "not_ready",
    "message": "The note isn't ready to save yet."
  }
}
```

<!-- capture: 23-save-empty-422.json response.body -->
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body",
        "transcript"
      ],
      "msg": "Value error, must be 1 to 20000 characters",
      "ctx": {
        "error": {}
      }
    }
  ]
}
```

### 3.7 Discard or delete: `DELETE /caregiver/voice-notes/{note_id}`
`204`, no body. Deletes the text, any stored audio and the row, in any status. A **saved** note can be deleted by the caregiver only while they still have access (the visit is live, or within 24 hours after it ended); after that it is `404`. A second delete is `404 not_found`: **treat it as success**.

<!-- capture: 31-delete-204.json response -->
```json
{
  "status_code": 204,
  "headers": {
    "cache-control": "no-store"
  },
  "body": null
}
```

<!-- capture: 32-delete-again-404.json response.body -->
```json
{
  "detail": {
    "code": "not_found",
    "message": "This note could not be found."
  }
}
```

### 3.8 The booker reads saved notes: `GET /patient/bookings/{booking_id}/voice-notes`
Not used by the caregiver app. For the patient app later. **Only `saved` notes**, newest first, as a smaller object (no `error`, `expires_at`, `consent_*`). `404 not_found` for anyone who is not the booker. Every read is audited.

<!-- capture: 33-patient-read-200.json response.body -->
```json
{
  "items": [
    {
      "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
      "booking_id": "1f11e2a9-74a9-4549-b376-7fdea2b9906b",
      "status": "saved",
      "language": "pcm",
      "transcript": "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was 120/80. She said she slept well.",
      "summary": "Placeholder client ate breakfast, took the morning tablets, blood pressure 120/80, slept well.",
      "summary_available": true,
      "edited": true,
      "duration_seconds": 42,
      "created_at": "2026-11-07T23:25:56.724137",
      "ready_at": "2026-11-07T23:25:56.995584",
      "saved_at": "2026-11-07T23:25:57.025029"
    }
  ]
}
```

<!-- capture: 34-patient-read-not-booker-404.json response.body -->
```json
{
  "detail": {
    "code": "not_found",
    "message": "This note could not be found."
  }
}
```

### 3.9 An admin reads saved notes: `GET /admin/bookings/{booking_id}/voice-notes`
Needs the `clinical.read` permission (Super Admin and Clinical Reviewer). The same smaller object. Audited.

<!-- capture: 35-admin-read-200.json response.body path=items first=1 -->
```json
[
  {
    "id": "25e7eee7-6a0d-45da-9b59-72a2fa0cc3d5",
    "booking_id": "1f11e2a9-74a9-4549-b376-7fdea2b9906b",
    "status": "saved",
    "language": "pcm",
    "transcript": "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was 120/80. She said she slept well.",
    "summary": "Placeholder client ate breakfast, took the morning tablets, blood pressure 120/80, slept well.",
    "summary_available": true,
    "edited": true,
    "duration_seconds": 42,
    "created_at": "2026-11-07T23:25:56.724137",
    "ready_at": "2026-11-07T23:25:56.995584",
    "saved_at": "2026-11-07T23:25:57.025029"
  }
]
```

<!-- capture: 36-admin-read-no-permission-403.json response -->
```json
{
  "status_code": 403,
  "headers": {
    "content-type": "application/json"
  },
  "body": {
    "detail": "Access denied. Missing permission: clinical.read"
  }
}
```

## 4. Every error shape

**Voice note errors have `detail` as an object:** `{"detail": {"code": "...", "message": "..."}}`. Branch on `detail.code`; the `message` is a sentence the app may show if it has none of its own. The exceptions, which have **no `detail.code`**: `401` (bad or missing token), `403` for a non-caregiver (`"Access denied. Caregiver account required."`), the admin `403` above (a string), and every `422` (FastAPI's list). The app's `ApiError.detail` reads only string details, so it must read the object itself.

| Status | `detail.code` | Message (exact) |
|---|---|---|
| 400 | `consent_required` | Confirm the client agreed before recording. |
| 400 | `consent_version_unknown` | Update the Oakly app to record notes. |
| 400 | `audio_empty` | The recording is empty. Please record again. |
| 404 | `not_found` | This note could not be found. |
| 409 | `visit_not_active` | Voice notes can be added during a visit. |
| 409 | `consent_missing` | Confirm the client agreed before recording. *(defensive; cannot normally happen, not captured)* |
| 409 | `already_processing` | This recording is already being processed. |
| 409 | `invalid_state` | This note changed. Please refresh. |
| 409 | `not_ready` | The note isn't ready to save yet. |
| 409 | `already_saved` | This note is already saved. |
| 409 | `audio_expired` | The recording is no longer stored. Please send it again. |
| 413 | `audio_too_large` | That recording is too long. Keep notes under 10 minutes. |
| 415 | `unsupported_audio_type` | This recording format isn't supported. |
| 429 | `note_limit_reached` | You have reached the limit of voice notes for this visit. *(20 per visit)* |
| 429 | `daily_limit_reached` | You have reached today's limit of voice notes. Please type your note. *(with `Retry-After`, seconds to UTC midnight)* |
| 503 | `transcription_unavailable` | Voice notes are unavailable right now. Please type your note. *(switched off, the day's capacity used, or the audio store is down)* |

<!-- capture: 38-create-visit-limit-429.json response.body -->
```json
{
  "detail": {
    "code": "note_limit_reached",
    "message": "You have reached the limit of voice notes for this visit."
  }
}
```

<!-- capture: 39-create-switched-off-503.json response.body -->
```json
{
  "detail": {
    "code": "transcription_unavailable",
    "message": "Voice notes are unavailable right now. Please type your note."
  }
}
```

Failure codes inside a **failed note** (`error.code`, arriving on a `200` poll, never as an HTTP error):

| `error.code` | `retryable` | Message (exact) |
|---|---|---|
| `transcription_failed` | true | We couldn't turn the recording into text. Try again. |
| `transcription_timeout` | true | Transcription took too long. Try again. |
| `provider_unavailable` | true | The voice service is busy. Try again in a moment. |
| `no_speech_detected` | false | We couldn't hear any speech. Record again, or type your note. |
| `audio_unsupported` | false | We couldn't read this recording. Record again, or type your note. |
| `provider_blocked` | false | We couldn't process this recording. Please type your note. |

Only `no_speech_detected` and `provider_unavailable` are in the captures (24, 26); the others are covered by backend tests, not captured.

## 5. Field-nesting and shape traps

- **`transcript` and `summary` are `null` until `ready`**, and `null` again on a `failed` note. Never assume a string.
- **`summary: null` with `summary_available: false` on a `ready` note is normal** (the summary step failed). A saved note whose summary was left empty has `summary: ""` and `summary_available: true`.
- **`error` is `null` except on a failed note**, where it is an object; do not confuse it with the HTTP error body, which has `detail`.
- **Timestamps are UTC with no `Z`** (`2026-11-07T23:25:56.724137`). Read them as UTC (`parseBackendDate`).
- **The same note shape comes from create, send, poll, retry and save** (202/200/201), but the **list wraps it** in `{"items": [...]}`, and the **patient and admin reads use a smaller object**.
- **`PUT` is used twice with different bodies:** `.../audio` (raw bytes) and `.../{id}` (JSON save). Do not mix them up.
- **Create answers `201` the first time and `200` for a repeat**; accept both.
- **`422` has no `Cache-Control: no-store` and no `detail.code`**; it never echoes what you sent.

## 6. Polling

After `PUT audio` (202), poll `GET .../voice-notes/{id}` every **2 seconds** for the first 10 reads, then every **5 seconds**. Stop when `status` is not `processing` (or `awaiting_audio`). The server gives a transcription up to about 90 seconds per try with up to 3 tries, so a note is usually `ready` within a minute and can take a few. After **3 minutes** say "This is taking longer than usual" and offer Check again and Discard; **do not keep a note spinning silently**. The sweeper fails a note stuck in `processing` after 5 minutes, which the next poll shows.

## 7. What the app must show the caregiver (UX consequences)

1. **Consent before the first recording of a visit.** Show exactly (version `2026-11-v1`; **a v1 draft that counsel must review before release**):
   > **Before you record** — [ ] The client has agreed to this visit being recorded. I understand Oakly sends the audio to an external AI service (Google Gemini) to turn it into text, and deletes the audio as soon as the text is ready. — *The text is stored securely and shared only with the client's care team and Oakly staff with clinical access.*

   Send `consent_confirmed: true` and `consent_version` with **every** create call. The server refuses a recording without it, and keeps the proof per recording.
2. **The transcript is an AI draft.** Say so on the review screen and ask the caregiver to check names, numbers and medicines. **Never save automatically.** Pidgin and accents may be transcribed wrongly.
3. **Delete the phone's copy of the recording** as soon as the note is `ready`, on discard and at sign-out; keep it until then (a retry may need it after `audio_expired`).
4. **Discard means gone on the server.** Say "The text is deleted and cannot be brought back" and call `DELETE`.
5. **A saved note cannot be edited.** To correct it, the caregiver deletes it (inside their window) and records again. There is no edit endpoint.
6. **After the caregiver's window, the saved note disappears from their app** (`404`). The booker still sees it.
7. **A note the caregiver has not saved disappears after 24 hours.** Say so on the "ready to check" card if it matters to the design.
8. **Limits to show before the server does:** 2 seconds minimum, 10 minutes maximum (stop the recorder at 10 minutes).
9. **When the feature is off (`503 transcription_unavailable`)**, hide the mic flow and tell the caregiver to type the note: "Voice notes are unavailable right now. Please type your note."

## 8. Verification

| Behaviour | Status |
|---|---|
| Create (201, repeat 200), consent and version refusals, 422s, visit-not-active, one 404 for no access | **verified live** (captures 01-08) |
| Raw-body audio send (202), empty, wrong type, wrong bytes, too large, second send, daily limit | **verified live** (11-16, 37) |
| Poll states: awaiting, processing, ready, ready without summary, failed (no speech, provider unavailable) | **verified live** (09, 17, 18, 24, 26, 30) |
| Retry (202), retry on a non-retryable note, retry after the audio expired | **verified live** (25, 27-29) |
| Save (200), same text again, different text, before ready, blank | **verified live** (10, 20-23) |
| Delete (204) and delete again (404), list | **verified live** (19, 31, 32) |
| Booker read (200, not the booker 404); admin read (200, 403 without `clinical.read`) | **verified live** (33-36) |
| Per-visit limit (429), switched off (503) | **verified live** (38, 39) |
| Failure codes `transcription_failed`, `transcription_timeout`, `audio_unsupported`, `provider_blocked` | **not captured**; covered by backend tests with `FakeGemini` |
| `consent_missing`, `invalid_state`, `daily` budget `503` | **not captured** (defensive or covered by tests) |
| Redis and the ARQ queue | **not in the captures**; real Redis + worker covered by `test_voice_notes_redis.py` |
| **The real Gemini call** with synthetic speech (2026-11-08): `audio/mp4` m4a and `audio/mpeg` accepted on `gemini-3.5-flash`, English and a Pidgin sentence transcribed, 3 to 4.4 s for 7 to 8 s of audio | **Verified live, but not through the API or a device**: the provider code was called directly (backend SOP, Verification); the captures in this guide still use `FakeGemini` |
| A real caregiver recording (accent, noise, phone codec), a note of minutes, Pidgin accuracy on a clinician-checked sample | **UNVERIFIED** |
| A real phone upload through the app | **UNVERIFIED** (no device run) |
