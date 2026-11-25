# FE integration guide: care alerts (admin queue; family messages)

| | |
|---|---|
| **Backend work package** | branch `feature/care-alerts`, migration `0035_care_alerts` (stacked on `feature/vitals-flags`); [SOP](sop/2026-11-25-care-alerts.md) |
| **Audience** | `apps/admin` (console): a **Care alerts** page. The caregiver and patient apps have **no new screen and no new call** in v1 (the family view is out of the v1 apps by the owner's decision); the family only receives a message. |
| **Base URL** | local test harness; every path starts with `/api/v1` |
| **Source of truth** | Every body below is **rendered from a capture** in [`docs/fe-captures/care-alerts/`](fe-captures/care-alerts/) written by `app/tests/test_capture_care_alerts.py` (the real app and a real Postgres, with the mail and push providers faked). Regenerate with `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-care-alerts.md docs/fe-captures/care-alerts`. All data is fake. |

## 1. What this is, in the console's terms

When a vital reading is out of range, or a care-plan task was not done by the end of a visit, the backend opens an **alert**. A person in the console works it: **acknowledge** ("I am on it") and **resolve** ("dealt with"), each with an optional note.

| Alert `type` | Raised when | `severity` |
|---|---|---|
| `vital_out_of_range` | a saved reading is High or Low (`warning`) or Critical (`critical`: an emergency, "call 112"). Rated by the same server engine as the vitals badges. | `warning` / `critical` |
| `task_missed` | a visit was completed and a task that was due in it (right day, right time slot) was never ticked | `warning` |

Alerts **debounce**: while the same patient has an alert of the same type and severity still open or acknowledged and recently fed, a new occurrence only raises `occurrence_count` and moves `last_occurred_at` (windows: warning reading 4 h, critical reading 30 min, missed task 12 h). `occurrence_count` above 1 means "this keeps happening"; show it.

**Permission:** every route needs `support.inbox` (Operations, Support, Clinical Reviewer and Super Admin hold it; Finance and Read-only Analyst get `403`). **Add the page to the console's navigation only for admins who hold `support.inbox`.**

## 2. Endpoints

| Method and path | Purpose |
|---|---|
| `GET /admin/care-alerts` | the queue. Query: `status` (`active` default = open + acknowledged, `open`, `acknowledged`, `resolved`, `all`), `severity`, `type`, `limit` (1-100, default 50), `offset` |
| `GET /admin/care-alerts/{id}` | one alert (same shape as a list item) |
| `POST /admin/care-alerts/{id}/acknowledge` | `open` -> `acknowledged`. Body `{"note": string \| null}` (note max 1000 characters) |
| `POST /admin/care-alerts/{id}/resolve` | `open` or `acknowledged` -> `resolved`. Same body |

Order of `items`: unresolved before resolved, critical before warning, open before acknowledged, newest activity first. `counts` is the whole queue, not the page: use it for the badge on the navigation item (`open`, and `critical_active` in red).

### 2.1 The list

<!-- capture: 010-list-active-super-admin.json response.body -->
```json
{
  "items": [
    {
      "id": "db889960-2efb-4962-9a2b-5f768630b841",
      "type": "vital_out_of_range",
      "severity": "critical",
      "status": "open",
      "summary": "Heart rate reading is at a critical level.",
      "created_at": "2026-11-25T18:51:38.926712",
      "last_occurred_at": "2026-11-25T18:51:38.926712",
      "occurrence_count": 1,
      "patient": {
        "full_name": "Ada Okafor",
        "phone": "+2340000719201",
        "masked": false
      },
      "care_recipient_name": null,
      "booking_id": null,
      "booking_code": null,
      "vital_id": "d9c8f9b0-e737-41a9-8361-25a5e615bb68",
      "family_notified_count": 1,
      "family_notified_at": "2026-11-25T18:51:38.939631",
      "admin_notified_at": "2026-11-25T18:51:38.941418",
      "acknowledged_by": null,
      "acknowledged_at": null,
      "resolved_by": null,
      "resolved_at": null,
      "note": null,
      "detail": {
        "vital_type": "heart_rate",
        "status": "critical",
        "reason": "Heart rate below 50 bpm. Contact the family and arrange an ambulance.",
        "value": "45",
        "unit": "bpm",
        "tasks": null,
        "days": null
      },
      "detail_masked": false
    },
    {
      "id": "be2adec3-4438-4d4d-915d-137e47e692dc",
      "type": "vital_out_of_range",
      "severity": "warning",
      "status": "open",
      "summary": "Temperature reading is higher than the safe range.",
      "created_at": "2026-11-25T18:51:38.902456",
      "last_occurred_at": "2026-11-25T18:51:38.902456",
      "occurrence_count": 1,
      "patient": {
        "full_name": "Bola Placeholder",
        "phone": "+2340000719202",
        "masked": false
      },
      "care_recipient_name": null,
      "booking_id": null,
      "booking_code": null,
      "vital_id": "551d981c-6a85-4be9-aab5-d97e00e385d7",
      "family_notified_count": 0,
      "family_notified_at": "2026-11-25T18:51:38.918764",
      "admin_notified_at": null,
      "acknowledged_by": null,
      "acknowledged_at": null,
      "resolved_by": null,
      "resolved_at": null,
      "note": null,
      "detail": {
        "vital_type": "temperature",
        "status": "high",
        "reason": "Temperature above 37.5 °C.",
        "value": "38.5",
        "unit": "°C",
        "tasks": null,
        "days": null
      },
      "detail_masked": false
    },
    {
      "id": "ed3b977a-554e-4bd6-bc48-7b3d2072b314",
      "type": "task_missed",
      "severity": "warning",
      "status": "open",
      "summary": "1 care task not marked done by the end of the visit.",
      "created_at": "2026-11-24T11:10:00",
      "last_occurred_at": "2026-11-24T11:10:00",
      "occurrence_count": 1,
      "patient": {
        "full_name": "Test Patient",
        "phone": "+2348965350251",
        "masked": false
      },
      "care_recipient_name": null,
      "booking_id": "6780e9a1-6a47-47ae-b5f8-e4eec9105202",
      "booking_code": "7226431C",
      "vital_id": null,
      "family_notified_count": 0,
      "family_notified_at": null,
      "admin_notified_at": null,
      "acknowledged_by": null,
      "acknowledged_at": null,
      "resolved_by": null,
      "resolved_at": null,
      "note": null,
      "detail": {
        "vital_type": null,
        "status": null,
        "reason": null,
        "value": null,
        "unit": null,
        "tasks": [
          "Placeholder task one"
        ],
        "days": [
          "2026-11-24"
        ]
      },
      "detail_masked": false
    }
  ],
  "total": 3,
  "counts": {
    "open": 3,
    "acknowledged": 0,
    "resolved": 0,
    "critical_active": 1
  }
}
```

**Traps (read these):**

- **One flat `detail` object serves both types.** For a reading, `vital_type`, `status` (`high`, `low`, `critical`), `reason`, `value`, `unit` are set and `tasks`, `days` are `null`; for a missed task it is the other way round. The keys are always present (present-but-null, never absent).
- **`detail` is `null` and `detail_masked` is `true` for an admin without `clinical.read`**; `note` is `null` for them too. `summary` is always there and is safe to show to anyone who can open the page.
- **`patient.masked`**: the name and phone are masked unless the admin holds `people.pii` (the same rule as the bookings console). For a visit booked for a relative, `patient` is the **booker** and `care_recipient_name` is the person the alert is about.
- **`booking_id` / `booking_code` are `null` for a reading alert** (a reading belongs to a patient, not to a visit); `vital_id` is `null` for a missed-task alert. `vital_id` can also become `null` later if the reading was deleted.
- **`family_notified_at` set with `family_notified_count` 0 means "tried, nobody could be reached"** (no consenting, verified family contact, or the provider failed). It is not an error. `family_notified_at` `null` means delivery has not run (yet). `admin_notified_at` is only ever set for `critical` (the admin e-mail).
- Timestamps are naive UTC (no `Z`), like every other timestamp in the API.

### 2.2 Masked view (an admin who holds only `support.inbox`)

<!-- capture: 011-list-active-support-masked.json response.body path=items.0 -->
```json
{
  "id": "db889960-2efb-4962-9a2b-5f768630b841",
  "type": "vital_out_of_range",
  "severity": "critical",
  "status": "open",
  "summary": "Heart rate reading is at a critical level.",
  "created_at": "2026-11-25T18:51:38.926712",
  "last_occurred_at": "2026-11-25T18:51:38.926712",
  "occurrence_count": 1,
  "patient": {
    "full_name": "Ada O.",
    "phone": "+234****9201",
    "masked": true
  },
  "care_recipient_name": null,
  "booking_id": null,
  "booking_code": null,
  "vital_id": "d9c8f9b0-e737-41a9-8361-25a5e615bb68",
  "family_notified_count": 1,
  "family_notified_at": "2026-11-25T18:51:38.939631",
  "admin_notified_at": "2026-11-25T18:51:38.941418",
  "acknowledged_by": null,
  "acknowledged_at": null,
  "resolved_by": null,
  "resolved_at": null,
  "note": null,
  "detail": null,
  "detail_masked": true
}
```

### 2.3 Filters

<!-- capture: 012-list-filtered.json observed keys=request -->
```json
{
  "request": {
    "method": "GET",
    "path": "/api/v1/admin/care-alerts?severity=warning&type=task_missed&limit=10",
    "auth": "Bearer <ADMIN_ACCESS_TOKEN_JWT>",
    "body": null
  }
}
```

### 2.4 Detail

For an admin with `clinical.read` (and no `people.pii`):

<!-- capture: 013-detail-clinical-reviewer.json response.body keys=patient,detail,detail_masked,note -->
```json
{
  "patient": {
    "full_name": "Ada O.",
    "phone": "+234****9201",
    "masked": true
  },
  "detail": {
    "vital_type": "heart_rate",
    "status": "critical",
    "reason": "Heart rate below 50 bpm. Contact the family and arrange an ambulance.",
    "value": "45",
    "unit": "bpm",
    "tasks": null,
    "days": null
  },
  "detail_masked": false,
  "note": null
}
```

A missed-task alert (the two timestamps are the capture's fixed clock; `family_notified_*` are null because that capture ran the sweep without the delivery step):

<!-- capture: 014-detail-missed-task.json response.body keys=type,summary,booking_code,detail -->
```json
{
  "type": "task_missed",
  "summary": "1 care task not marked done by the end of the visit.",
  "booking_code": "7226431C",
  "detail": {
    "vital_type": null,
    "status": null,
    "reason": null,
    "value": null,
    "unit": null,
    "tasks": [
      "Placeholder task one"
    ],
    "days": [
      "2026-11-24"
    ]
  }
}
```

**Opening a detail with the clinical `detail` visible writes a `care_alert.read` row in the audit log.** Do not prefetch details in a list view; open one when the admin opens it.

### 2.5 Acknowledge and resolve

<!-- capture: 020-acknowledge.json observed keys=request -->
```json
{
  "request": {
    "method": "POST",
    "path": "/api/v1/admin/care-alerts/<ALERT_ID>/acknowledge",
    "auth": "Bearer <ADMIN_ACCESS_TOKEN_JWT>",
    "body": {
      "note": "Phoned the family; ambulance called (placeholder note)."
    }
  }
}
```

<!-- capture: 020-acknowledge.json response.body keys=status,acknowledged_by,acknowledged_at,note,resolved_by -->
```json
{
  "status": "acknowledged",
  "acknowledged_by": {
    "id": "021ae30b-b641-4398-86a2-b5e5fe70cfb7",
    "email": "super_admin@example.test"
  },
  "acknowledged_at": "2026-11-25T18:51:39.567269",
  "note": "Phoned the family; ambulance called (placeholder note).",
  "resolved_by": null
}
```

<!-- capture: 022-resolve.json response.body keys=status,acknowledged_by,resolved_by,resolved_at,note -->
```json
{
  "status": "resolved",
  "acknowledged_by": {
    "id": "021ae30b-b641-4398-86a2-b5e5fe70cfb7",
    "email": "super_admin@example.test"
  },
  "resolved_by": {
    "id": "021ae30b-b641-4398-86a2-b5e5fe70cfb7",
    "email": "super_admin@example.test"
  },
  "resolved_at": "2026-11-25T18:51:39.582655",
  "note": "Patient is at hospital (placeholder note)."
}
```

Both return the full updated alert. An open alert can be resolved without being acknowledged (capture `024`: `acknowledged_by` stays `null`). The acknowledgement stays on a resolved alert.

## 3. Errors

| Case | Status | Body |
|---|---|---|
| Acknowledge an acknowledged alert | `409` | see below (`already_acknowledged`) |
| Resolve or acknowledge a resolved alert | `409` | see below (`already_resolved`) |

<!-- capture: 021-acknowledge-again-409.json response.body -->
```json
{
  "detail": {
    "code": "already_acknowledged",
    "message": "This alert has already been acknowledged."
  }
}
```

<!-- capture: 023-resolve-again-409.json response.body -->
```json
{
  "detail": {
    "code": "already_resolved",
    "message": "This alert has already been resolved."
  }
}
```

Handle both by **refreshing the row**: someone else got there first. They are `detail` **objects** with `code` and `message`; the 404 and 403 below carry a plain string.

<!-- capture: 030-not-found-404.json response.body -->
```json
{
  "detail": "Care alert not found"
}
```

<!-- capture: 031-finance-403.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: support.inbox"
}
```

Validation errors never echo the rejected value:

<!-- capture: 033-note-too-long-422.json response.body -->
```json
{
  "detail": [
    {
      "type": "string_too_long",
      "loc": [
        "body",
        "note"
      ],
      "msg": "String should have at most 1000 characters",
      "ctx": {
        "max_length": 1000
      }
    }
  ]
}
```

<!-- capture: 032-bad-status-422.json response.body -->
```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": [
        "query",
        "status"
      ],
      "msg": "Input should be 'active', 'open', 'acknowledged', 'resolved' or 'all'",
      "ctx": {
        "expected": "'active', 'open', 'acknowledged', 'resolved' or 'all'"
      }
    }
  ]
}
```

No token is `401 {"detail": "Not authenticated"}` as on every admin route.

## 4. Suggested console behaviour

- **List, default `status=active`**, newest activity at the top as the server returns it. Poll every 30 to 60 seconds while the page is open (there is no push to the console); the navigation badge can poll `GET /admin/care-alerts?limit=1` and read `counts`.
- **Critical rows are red and show `summary` plus the call-112 context**; the server's e-mail to the on-call admins says only that a critical alert is waiting (it names nobody), so the console is where the name and phone are read (subject to `people.pii`).
- Acknowledge and Resolve each open a small note box (optional, 1000 characters). Disable both buttons while the request is in flight and on `409` refresh the row.
- Show `occurrence_count` when above 1 ("4 readings in the window"), and `last_occurred_at`.

## 5. What the family receives (no FE work, for the copy review)

A contact is told only when **all** hold: the patient (or the care recipient) has a family or guardian contact with clinical access, the latest "share with family" consent is granted, the contact's account is active, and they have a verified e-mail or a registered app device. **Nobody else is ever told**, and WhatsApp is not used. The message is the first name and one sentence; never a reading, a surname, a task name, a note or a condition. These are the real messages from the capture run:

<!-- capture: 001-family-email-critical.json observed -->
```json
{
  "observed": {
    "from": "Oakly <noreply@example.test>",
    "to": "<FAMILY_EMAIL>",
    "subject": "Oaklycare - Oakly care alert",
    "message_in_body": "A heart rate reading for Ada is at a dangerous level. This may be an emergency. Call 112 now."
  }
}
```

<!-- capture: 002-family-push-critical.json observed -->
```json
{
  "observed": {
    "title": "Oakly care alert",
    "body": "A heart rate reading for Ada is at a dangerous level. This may be an emergency. Call 112 now.",
    "data": {
      "type": "care_alert",
      "severity": "critical"
    },
    "sound": "default",
    "priority": "high"
  }
}
```

A warning reading says "A {vital} reading for {first name} was higher|lower than the safe range. Please check in with {first name} or their doctor." (covered by `test_care_alerts_vitals.py`, not captured separately). A missed task says "A care task planned for {first name}'s visit was not marked as done." (or "Some care tasks ... were ..."). The admin e-mail for a critical alert:

<!-- capture: 003-admin-email-critical.json observed -->
```json
{
  "observed": {
    "to": "<ADMIN_EMAIL>",
    "subject": "Oaklycare - Critical care alert waiting",
    "message_in_body": "A critical care alert is open in the admin console and needs a person to look at it now."
  }
}
```

## 6. Not exercised live

| Item | Status |
|---|---|
| A real e-mail in an inbox, a real push on a phone | Not done: Resend and Expo are faked in the capture and the tests (the exact payloads above are what is handed to them) |
| The 5-minute cron that judges finished visits (`care_alert_sweep_job`) | The sweep function is run directly in the tests and the capture; the cron firing on a real worker is not exercised |
| A missed-task alert reaching the family | Covered by tests (`test_care_alerts_missed_tasks.py`), not in a capture |
| Console page | Built in the monorepo; see its PR for what was checked there |

## 7. Verification table

| Behaviour | Verified |
|---|---|
| Critical reading raises a critical alert; the family gets e-mail and push; admins with `support.inbox` get the e-mail | live (captures 001 to 003, 010) |
| Missed task alert and its `detail` | live (010, 014) |
| Masking by `people.pii` and `clinical.read` | live (011, 013) |
| Acknowledge, resolve, resolve-without-ack | live (020, 022, 024) |
| `409` shapes, `404`, `403`, `422` | live (021, 023, 030 to 033) |
| Debounce windows and escalation (a critical is never absorbed by a warning) | test only (`test_care_alerts_vitals.py`) |
| Consent, verification and role rules for the family | test only |
| Audit rows (`care_alert.acknowledge`, `.resolve`, `.read`) | test only (`test_admin_care_alerts.py`) |
