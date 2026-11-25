# FE integration guide: care templates, the caregiver's own documentation, the nurse's 2-hour note window

| | |
|---|---|
| **Backend work package** | branch `feature/care-templates-docs-space`, migration `0031_care_templates_docs_space`; [SOP](sop/2026-11-25-care-templates-docs-space.md) |
| **Audience** | `apps/caregiver` (visit notes screen, care plan row, care plan details, My documentation) and `apps/admin` (Clinical > Caregiver documentation) |
| **Base URL** | local test harness; every path starts with `/api/v1` |
| **Source of truth** | Every body below is **rendered from a capture** in [`docs/fe-captures/care-templates-docs-space/`](fe-captures/care-templates-docs-space/) written by `app/tests/test_capture_care_templates_docs_space.py` (run against the real app and a real Postgres). Long lists are trimmed and the capture's own note says so. Names and ids are placeholders. |
| **Read this first** | section 7 (what the user will feel) and section 8 (what is verified live and what is not) |

Four things changed. None of them removes a field the apps already read; every new field has a default the old apps ignore.

1. **Care plan template text** (clinician review pack, section 1). Four templates were rewritten and are now `version: 2`. An **Elderly** template can be used by any caregiver, not only a nurse.
2. **The caregiver's own documentation space**: private notes for a visit. Not shown to the family. New endpoints.
3. **The assigned nurse's 2-hour grace** to add care-plan notes after the visit is completed.
4. **Admin console**: a read-only, audited list of those private notes.

The medication notice is unchanged (Oakly does not remind anyone). There are no drug reminders and no wallet in this work.

## 1. Template text

### 1.1 What changed in the text

Only the four templates the clinical lead approved "as annotated" changed. Plans made earlier keep their own copy of the text; `plan.template.version` says which version a plan started from.

| Template (`key`) | Version | Change |
|---|---|---|
| `stroke_recovery` | 2 | adds the line "Contact the family and inform the family's doctor" |
| `diabetes_management` | 2 | meal task becomes "Meal support, note what was eaten and document"; the warning becomes "Report dizziness, low or high blood glucose, sweating or unusual drowsiness at once"; adds the family-and-doctor line |
| `post_surgery_recovery` | 2 | adds the pain task "Ask about pain and record it" (morning and evening, every day) that the goal "Pain reported and recorded" was missing; adds the family-and-doctor line |
| `cardiac_rehabilitation` | 2 | activity task becomes "Light activity as instructed by care team or following post-hospital discharge instructions"; adds the family-and-doctor line |
| `elderly_daily_living_dementia` | 1 | text unchanged; now `caregiver_can_use: true` |

`clinical_review_status` is **still `pending` for all five**: the migration does not sign anything off in anyone's name. Sign-off is the admin API `POST /admin/care-plan-templates/{id}/review` (permission `careplans.templates`); there is no console screen for it yet. Keep showing "Not yet clinically reviewed" while it is `pending`.

The pain "record" is the visit note: pain is not one of the six vitals, so the task is ticked and the detail goes in a note (or the caregiver's own documentation).

`GET /api/v1/caregiver/care-plan-templates/0d0fdf9e-52e5-4a3b-965f-656587415f7d` returns **200**:

<!-- capture: 003-nurse-template-post-surgery.json response.body -->
```json
{
  "id": "0d0fdf9e-52e5-4a3b-965f-656587415f7d",
  "key": "post_surgery_recovery",
  "name": "Post-Surgery Recovery",
  "summary": "Wound care, comfort, gentle movement and nutrition while healing after an operation.",
  "duration_days": 21,
  "review_every_days": 7,
  "default_risk_level": "medium",
  "clinical_review_status": "pending",
  "version": 2,
  "caregiver_can_use": false,
  "goals": [
    {
      "title": "Wound kept clean and dry as instructed",
      "target": null,
      "frequency": "daily"
    },
    {
      "title": "Pain reported and recorded",
      "target": null,
      "frequency": "daily"
    },
    {
      "title": "Gradual return to movement",
      "target": null,
      "frequency": "ongoing"
    },
    {
      "title": "Adequate fluids and nutrition",
      "target": null,
      "frequency": "daily"
    }
  ],
  "tasks": [
    {
      "title": "Wound check and record appearance",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Wound check and record appearance",
      "time_slot": "evening",
      "time_of_day": "18:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Assisted short walk",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Assisted short walk",
      "time_slot": "afternoon",
      "time_of_day": "13:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Hygiene and clean clothing",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Fluids and meal support",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Fluids and meal support",
      "time_slot": "afternoon",
      "time_of_day": "13:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Fluids and meal support",
      "time_slot": "evening",
      "time_of_day": "18:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Ask about pain and record it",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "title": "Ask about pain and record it",
      "time_slot": "evening",
      "time_of_day": "18:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    }
  ],
  "special_instructions": [
    "Wound care exactly as the surgical team instructed",
    "Report fever, spreading redness or discharge at once",
    "Contact the family and inform the family's doctor"
  ]
}
```

*Version 2: the pain task and the family-and-doctor line.*

### 1.2 Who sees which templates

`GET /caregiver/care-plan-templates` used to be `403` for anyone without the nurse skill. **It is now `200` for every caregiver**: a nurse gets all five, anyone else gets only the templates with `caregiver_can_use: true` (today: Elderly).

`GET /api/v1/caregiver/care-plan-templates` returns **200**:

<!-- capture: 001-nurse-templates-list.json response.body -->
```json
{
  "items": [
    {
      "id": "3aa70b63-2b40-4f74-9e8d-4fd43c909c1c",
      "key": "cardiac_rehabilitation",
      "name": "Cardiac Rehabilitation",
      "summary": "Light activity, resting readings and low-salt meals while recovering from a heart event.",
      "duration_days": 45,
      "review_every_days": 14,
      "default_risk_level": "high",
      "clinical_review_status": "pending",
      "version": 2,
      "caregiver_can_use": false
    },
    {
      "id": "a96d8aba-6ada-457c-8aaf-67fc14091df1",
      "key": "diabetes_management",
      "name": "Diabetes Management",
      "summary": "Meals, readings, foot care and gentle activity for a client living with diabetes.",
      "duration_days": 30,
      "review_every_days": 14,
      "default_risk_level": "medium",
      "clinical_review_status": "pending",
      "version": 2,
      "caregiver_can_use": false
    },
    {
      "id": "5f437e3b-d9c0-4150-9fc2-afc2f3b7ed24",
      "key": "elderly_daily_living_dementia",
      "name": "Elderly Daily Living and Dementia Support",
      "summary": "A calm daily routine with meals, fluids, safety and engagement for an older client or a client with dementia.",
      "duration_days": 30,
      "review_every_days": 14,
      "default_risk_level": "medium",
      "clinical_review_status": "pending",
      "version": 1,
      "caregiver_can_use": true
    },
    {
      "id": "0d0fdf9e-52e5-4a3b-965f-656587415f7d",
      "key": "post_surgery_recovery",
      "name": "Post-Surgery Recovery",
      "summary": "Wound care, comfort, gentle movement and nutrition while healing after an operation.",
      "duration_days": 21,
      "review_every_days": 7,
      "default_risk_level": "medium",
      "clinical_review_status": "pending",
      "version": 2,
      "caregiver_can_use": false
    },
    {
      "id": "9a7ee442-10c3-4c20-8339-8929f6365e9f",
      "key": "stroke_recovery",
      "name": "Stroke Recovery",
      "summary": "Daily support after a stroke: safe movement, therapy exercises, swallowing and skin care.",
      "duration_days": 30,
      "review_every_days": 14,
      "default_risk_level": "high",
      "clinical_review_status": "pending",
      "version": 2,
      "caregiver_can_use": false
    }
  ]
}
```

*A nurse sees all five. Four are version 2 (clinician-approved text); Elderly is version 1 and `caregiver_can_use`.*

`GET /api/v1/caregiver/care-plan-templates` returns **200**:

<!-- capture: 002-caregiver-templates-list.json response.body -->
```json
{
  "items": [
    {
      "id": "5f437e3b-d9c0-4150-9fc2-afc2f3b7ed24",
      "key": "elderly_daily_living_dementia",
      "name": "Elderly Daily Living and Dementia Support",
      "summary": "A calm daily routine with meals, fluids, safety and engagement for an older client or a client with dementia.",
      "duration_days": 30,
      "review_every_days": 14,
      "default_risk_level": "medium",
      "clinical_review_status": "pending",
      "version": 1,
      "caregiver_can_use": true
    }
  ]
}
```

*A caregiver without the nurse skill sees only the template flagged `caregiver_can_use` (it was a 403 before).*

A nurse-only template asked for by a non-nurse is still refused, with the same coded body as every other `not_a_nurse`:

`GET /api/v1/caregiver/care-plan-templates/9a7ee442-10c3-4c20-8339-8929f6365e9f` returns **403**:

<!-- capture: 004-caregiver-template-nurse-only.json response.body -->
```json
{
  "detail": {
    "code": "not_a_nurse",
    "message": "Only nurses can create or change a care plan."
  }
}
```

*A nurse-only template, asked for by a caregiver without the nurse skill.*

**Field to read:** `caregiver_can_use` is on every list item and on the preview (`TemplateDetail`). It is `false` on a template a non-nurse cannot use.

## 2. Who can start and edit a plan

### 2.1 `author_scope` on the visit summary

`GET /caregiver/bookings/{booking_id}/care-plan` now carries `author_scope`. `can_author` keeps its meaning (a nurse, and a client who can have a plan).

| Caller | `can_author` | `author_scope` | What the app offers |
|---|---|---|---|
| nurse | `true` | `full` | Create care plan (blank) and Use a template (any) |
| any other caregiver | `false` | `caregiver_templates` | only the templates `GET /care-plan-templates` returned for them |

`GET /api/v1/caregiver/bookings/49727168-3c98-45f7-ae87-95a143dfc6e7/care-plan` returns **200**:

<!-- capture: 010-nurse-visit-summary.json response.body -->
```json
{
  "booking_id": "49727168-3c98-45f7-ae87-95a143dfc6e7",
  "subject_ready": true,
  "can_author": true,
  "author_scope": "full",
  "plan": null
}
```

*`author_scope: full` for a nurse.*

`GET /api/v1/caregiver/bookings/330cd2e4-726b-4f39-89f1-4119d7fe521b/care-plan` returns **200**:

<!-- capture: 011-caregiver-visit-summary.json response.body -->
```json
{
  "booking_id": "330cd2e4-726b-4f39-89f1-4119d7fe521b",
  "subject_ready": true,
  "can_author": false,
  "author_scope": "caregiver_templates",
  "plan": null
}
```

*`author_scope: caregiver_templates` and `can_author: false` for a caregiver without the nurse skill.*

### 2.2 A caregiver who is not a nurse, on the Elderly template

`POST /caregiver/bookings/{booking_id}/care-plan` with the Elderly `template_id` is `201` for any caregiver on a live visit. A blank plan, or any other template, is `403 not_a_nurse`.

`POST /api/v1/caregiver/bookings/330cd2e4-726b-4f39-89f1-4119d7fe521b/care-plan` returns **201** for this body:

<!-- capture: 013-caregiver-create-from-elderly.json request.body -->
```json
{
  "template_id": "5f437e3b-d9c0-4150-9fc2-afc2f3b7ed24",
  "diagnosis": "Placeholder diagnosis"
}
```

Response:

<!-- capture: 013-caregiver-create-from-elderly.json response.body -->
```json
{
  "id": "6b522513-a4d9-4336-8881-1c5f5564b208",
  "version": 1,
  "status": "active",
  "booking_id": "330cd2e4-726b-4f39-89f1-4119d7fe521b",
  "client": {
    "full_name": "Placeholder Client",
    "age": 71,
    "gender": "female"
  },
  "diagnosis": "Placeholder diagnosis",
  "risk_level": "medium",
  "start_date": "2026-11-25",
  "duration_days": 30,
  "review_every_days": 14,
  "review_due_on": "2026-12-09",
  "template": {
    "id": "5f437e3b-d9c0-4150-9fc2-afc2f3b7ed24",
    "name": "Elderly Daily Living and Dementia Support",
    "version": 1
  },
  "author": {
    "name": "Placeholder Caregiver Chidi",
    "kind": "caregiver"
  },
  "allergies": [],
  "special_instructions": [
    "Do not leave the client alone if wandering is a risk",
    "Note any new confusion or change in behaviour"
  ],
  "goals": [
    {
      "id": "09a32137-0d15-4399-b627-b5487f672150",
      "title": "Calm, predictable daily routine",
      "target": null,
      "frequency": "daily",
      "status": "active"
    },
    {
      "id": "fba58548-8e12-414f-b4ed-9a22988d0267",
      "title": "Safe home and fall prevention",
      "target": null,
      "frequency": "ongoing",
      "status": "active"
    },
    {
      "id": "cc1d183d-a9d9-44a0-bf05-7bb680433bbb",
      "title": "Regular meals, fluids and toileting",
      "target": null,
      "frequency": "daily",
      "status": "active"
    },
    {
      "id": "7a5e6546-1b7f-4f40-a7ac-7c68c1bf8d5c",
      "title": "Keep the client engaged",
      "target": null,
      "frequency": "daily",
      "status": "active"
    }
  ],
  "medications": [],
  "tasks": [
    {
      "id": "f4008090-5dff-4ab7-baa6-eed43fe3b51a",
      "title": "Morning routine: wash, dress, breakfast",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "id": "d7dcf244-54c2-4197-9175-200e6b0cb439",
      "title": "Hydration prompts",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "id": "fc0f0ca7-6465-4c84-b5fb-0265ac9ccceb",
      "title": "Hydration prompts",
      "time_slot": "afternoon",
      "time_of_day": "13:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    }
  ],
  "capabilities": {
    "can_edit_plan": true,
    "can_edit_medications": false,
    "can_tick_tasks": true,
    "can_add_observation": true
  },
  "observations_close_at": null,
  "created_at": "2026-11-25T17:41:52.598313",
  "updated_at": "2026-11-25T17:41:52.598314"
}
```

*`capabilities.can_edit_plan` is true; `can_edit_medications` is false. `tasks` trimmed to its first 3 items; the real list is longer.*

`POST /api/v1/caregiver/bookings/330cd2e4-726b-4f39-89f1-4119d7fe521b/care-plan` returns **403** for this body:

<!-- capture: 012-caregiver-create-blank-refused.json request.body -->
```json
{
  "diagnosis": "Placeholder diagnosis",
  "risk_level": "low"
}
```

Response:

<!-- capture: 012-caregiver-create-blank-refused.json response.body -->
```json
{
  "detail": {
    "code": "not_a_nurse",
    "message": "Only nurses can create or change a care plan."
  }
}
```

After that the caregiver may edit the plan (header, goals, tasks) and so may **any caregiver on the same client**, because the plan came from a caregiver-usable template. Plans from the other four templates, and blank plans, stay nurse-only to edit.

`PATCH /api/v1/caregiver/care-plans/6b522513-a4d9-4336-8881-1c5f5564b208` returns **200** for this body:

<!-- capture: 015-caregiver-edit-elderly-plan.json request.body -->
```json
{
  "expected_version": 1,
  "diagnosis": "Placeholder diagnosis, updated"
}
```

Response:

<!-- capture: 015-caregiver-edit-elderly-plan.json response.body -->
```json
{
  "id": "6b522513-a4d9-4336-8881-1c5f5564b208",
  "version": 2,
  "status": "active",
  "booking_id": "330cd2e4-726b-4f39-89f1-4119d7fe521b",
  "client": {
    "full_name": "Placeholder Client",
    "age": 71,
    "gender": "female"
  },
  "diagnosis": "Placeholder diagnosis, updated",
  "risk_level": "medium",
  "start_date": "2026-11-25",
  "duration_days": 30,
  "review_every_days": 14,
  "review_due_on": "2026-12-09",
  "template": {
    "id": "5f437e3b-d9c0-4150-9fc2-afc2f3b7ed24",
    "name": "Elderly Daily Living and Dementia Support",
    "version": 1
  },
  "author": {
    "name": "Placeholder Caregiver Chidi",
    "kind": "caregiver"
  },
  "allergies": [],
  "special_instructions": [
    "Do not leave the client alone if wandering is a risk",
    "Note any new confusion or change in behaviour"
  ],
  "goals": [
    {
      "id": "09a32137-0d15-4399-b627-b5487f672150",
      "title": "Calm, predictable daily routine",
      "target": null,
      "frequency": "daily",
      "status": "active"
    },
    {
      "id": "fba58548-8e12-414f-b4ed-9a22988d0267",
      "title": "Safe home and fall prevention",
      "target": null,
      "frequency": "ongoing",
      "status": "active"
    }
  ],
  "medications": [],
  "tasks": [
    {
      "id": "f4008090-5dff-4ab7-baa6-eed43fe3b51a",
      "title": "Morning routine: wash, dress, breakfast",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    },
    {
      "id": "d7dcf244-54c2-4197-9175-200e6b0cb439",
      "title": "Hydration prompts",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        2,
        3,
        4,
        5,
        6,
        7
      ]
    }
  ],
  "capabilities": {
    "can_edit_plan": true,
    "can_edit_medications": false,
    "can_tick_tasks": true,
    "can_add_observation": true
  },
  "observations_close_at": null,
  "created_at": "2026-11-25T17:41:52.598313",
  "updated_at": "2026-11-25T17:41:52.685561"
}
```

*`goals` trimmed to its first 2 items; the real list is longer.*

**Medicines stay nurse-only, even on that plan.** The capability `can_edit_medications` is `false` for a non-nurse, and the call is refused:

`POST /api/v1/caregiver/care-plans/6b522513-a4d9-4336-8881-1c5f5564b208/medications` returns **403** for this body:

<!-- capture: 014-caregiver-add-medication-refused.json request.body -->
```json
{
  "name": "Placeholder medicine",
  "frequency": "once_daily",
  "start_date": "2026-11-25"
}
```

Response:

<!-- capture: 014-caregiver-add-medication-refused.json response.body -->
```json
{
  "detail": {
    "code": "not_a_nurse",
    "message": "Only nurses can create or change a care plan."
  }
}
```

*Medicines stay nurse-only, even on a plan the caregiver may edit.*

**UX trap:** that `403` carries the message "Only nurses can create or change a care plan." Do not show it as it is for a medicine; hide the Add medication control when `capabilities.can_edit_medications` is `false` (the app does).

### 2.3 Capabilities, now four fields

`capabilities` on a plan: `can_edit_plan`, `can_edit_medications` (new), `can_tick_tasks`, `can_add_observation`. Draw the controls from these, not from the nurse skill. During the nurse's post-visit window (section 3) only `can_add_observation` can be `true`.

## 3. The assigned nurse's 2-hour grace for care-plan notes

Before: a care-plan note (`POST /caregiver/care-plans/{id}/observations`) needed a visit **in progress**. Now the **assigned nurse** (the nurse on that booking, still approved) may also add one for **2 hours after the visit was completed**. The window is the one typed and voice notes already use (`VOICE_NOTE_POST_VISIT_GRACE_MINUTES`, 120), counted from the visit's recorded end (`actual_end_time`, else the scheduled end). Other caregivers are unchanged: they lose access to the plan when the visit completes.

What the nurse can do inside the window, and what they still cannot:

| Call | During the 2 h | After it |
|---|---|---|
| `GET /bookings/{booking_id}/care-plan` (visit summary) | `200`, `can_author: false` | `404` `"Care plan not found"` |
| `GET /care-plans/{id}` | `200`; `capabilities.can_add_observation: true`, the other three `false`; `observations_close_at` set | `404` |
| `GET /care-plans/{id}/observations` | `200` | `404` |
| `POST /care-plans/{id}/observations` | `201` | `404` |
| edit the plan, goals, tasks, medicines; tick a task | `404` (needs a live visit) | `404` |

**This is how the app finds the plan after the visit:** the visit summary now answers the nurse for 2 hours, with `plan.id`; open `CarePlanDetails` with it.

`GET /api/v1/caregiver/care-plans/9886e885-1dbb-41a0-b9e1-1c8845b7c8da` returns **200**:

<!-- capture: 020-nurse-plan-live-visit.json response.body path=capabilities -->
```json
{
  "can_edit_plan": true,
  "can_edit_medications": true,
  "can_tick_tasks": true,
  "can_add_observation": true
}
```

*During the visit: `observations_close_at` is null.*

`GET /api/v1/caregiver/bookings/49727168-3c98-45f7-ae87-95a143dfc6e7/care-plan` returns **200**:

<!-- capture: 021-nurse-visit-summary-grace.json response.body -->
```json
{
  "booking_id": "49727168-3c98-45f7-ae87-95a143dfc6e7",
  "subject_ready": true,
  "can_author": false,
  "author_scope": "full",
  "plan": {
    "id": "9886e885-1dbb-41a0-b9e1-1c8845b7c8da",
    "version": 1,
    "status": "active",
    "diagnosis": "Placeholder diagnosis alpha",
    "risk_level": "high",
    "start_date": "2026-11-25",
    "duration_days": 30,
    "review_every_days": 14,
    "review_due_on": "2026-12-09",
    "goal_count": 1,
    "task_count": 1
  }
}
```

*30 minutes after the visit was completed. `can_author` is false (a plan is created during a live visit).*

`GET /api/v1/caregiver/care-plans/9886e885-1dbb-41a0-b9e1-1c8845b7c8da` returns **200**:

<!-- capture: 022-nurse-plan-grace.json response.body -->
```json
{
  "id": "9886e885-1dbb-41a0-b9e1-1c8845b7c8da",
  "version": 1,
  "status": "active",
  "booking_id": "49727168-3c98-45f7-ae87-95a143dfc6e7",
  "client": {
    "full_name": "Placeholder Client",
    "age": 71,
    "gender": "female"
  },
  "diagnosis": "Placeholder diagnosis alpha",
  "risk_level": "high",
  "start_date": "2026-11-25",
  "duration_days": 30,
  "review_every_days": 14,
  "review_due_on": "2026-12-09",
  "template": null,
  "author": {
    "name": "Placeholder Nurse Ngozi",
    "kind": "caregiver"
  },
  "allergies": [
    "Placeholder allergen beta"
  ],
  "special_instructions": [
    "Placeholder instruction gamma"
  ],
  "goals": [
    {
      "id": "4d115132-d4e0-480b-8997-09a64d826a92",
      "title": "Placeholder goal delta",
      "target": "Placeholder target epsilon",
      "frequency": "daily",
      "status": "active"
    }
  ],
  "medications": [],
  "tasks": [
    {
      "id": "d1da9f6f-c2a0-40e1-a87d-f31dd3acda74",
      "title": "Placeholder task zeta",
      "time_slot": "morning",
      "time_of_day": "08:00",
      "days_of_week": [
        1,
        3,
        5
      ]
    }
  ],
  "capabilities": {
    "can_edit_plan": false,
    "can_edit_medications": false,
    "can_tick_tasks": false,
    "can_add_observation": true
  },
  "observations_close_at": "2026-11-25T19:11:52.706643",
  "created_at": "2026-11-25T17:41:52.695929",
  "updated_at": "2026-11-25T17:41:52.695930"
}
```

*30 minutes after completion: only `can_add_observation` is true; `observations_close_at` is the end of the window.*

`POST /api/v1/caregiver/care-plans/9886e885-1dbb-41a0-b9e1-1c8845b7c8da/observations` returns **201** for this body:

<!-- capture: 023-nurse-note-in-grace.json request.body -->
```json
{
  "body": "Placeholder: client rested well after the visit."
}
```

Response:

<!-- capture: 023-nurse-note-in-grace.json response.body -->
```json
{
  "id": "c5d87653-341f-45df-bd46-9571a8e7bc72",
  "category": "observation",
  "body": "Placeholder: client rested well after the visit.",
  "author": {
    "name": "Placeholder Nurse Ngozi",
    "kind": "caregiver"
  },
  "created_at": "2026-11-25T17:41:52.731292"
}
```

Editing is refused (the plain `404`, a **string** `detail`, not a coded object):

`PATCH /api/v1/caregiver/care-plans/9886e885-1dbb-41a0-b9e1-1c8845b7c8da` returns **404** for this body:

<!-- capture: 024-nurse-edit-in-grace-refused.json request.body -->
```json
{
  "expected_version": 1,
  "diagnosis": "Placeholder"
}
```

Response:

<!-- capture: 024-nurse-edit-in-grace-refused.json response.body -->
```json
{
  "detail": "Care plan not found"
}
```

*Editing still needs a live visit.*

And after the window:

`POST /api/v1/caregiver/care-plans/9886e885-1dbb-41a0-b9e1-1c8845b7c8da/observations` returns **404** for this body:

<!-- capture: 025-nurse-note-after-grace.json request.body -->
```json
{
  "body": "Placeholder: client rested well after the visit."
}
```

Response:

<!-- capture: 025-nurse-note-after-grace.json response.body -->
```json
{
  "detail": "Care plan not found"
}
```

*125 minutes after completion: the same 404 as any lost access.*

**Field-nesting traps**
- `observations_close_at` is at the **top level of the plan**, next to `capabilities`, not inside it. It is `null` during a live visit (there is no end yet). The admin plan (`GET /admin/care-plans/{id}`) does not carry it at all.
- All timestamps are **naive UTC** with no `Z` (`"2026-11-25T19:11:52.706643"`): treat a missing zone as UTC.

## 4. The caregiver's own documentation

A private place where a caregiver records their own notes for a visit. **Any caregiver on the visit may use it, not only a nurse. It is never shown to the family** (owner decision, 2026-11-25): no patient or family endpoint returns it, and the booking and patient payloads do not carry it. Only the author and an admin with `careplans.read` can read it. It is separate from the visit note (`/booking/notes/...`), which the patient can see, and from the care-plan notes.

Rules: **append-only** (no edit, no delete; a correction is a new entry), text 1 to 4000 characters, encrypted at rest. It follows the visit-note windows: write while the visit is in progress and for **2 hours after completion** (`VOICE_NOTE_POST_VISIT_GRACE_MINUTES`); the author can read their own entries until **24 hours after the visit ended** (`VOICE_NOTE_EDIT_WINDOW_HOURS`), then `404`.

### 4.1 Endpoints (caregiver, Bearer token of a caregiver account)

| Method and path | Purpose |
|---|---|
| `POST /caregiver/bookings/{booking_id}/documentation` | add an entry. Body `{"body": "<text>"}`. `201` |
| `GET /caregiver/bookings/{booking_id}/documentation` | the caller's own entries for the visit, newest first, plus `can_add`, `closes_at`, `readable_until` |

`POST /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **201** for this body:

<!-- capture: 030-caregiver-documentation-add.json request.body -->
```json
{
  "body": "Placeholder: gave breakfast at 08:30, client walked in the garden."
}
```

Response:

<!-- capture: 030-caregiver-documentation-add.json response.body -->
```json
{
  "id": "622c1ed3-6c9f-4a0a-a08a-3dfb033edf1a",
  "booking_id": "efdbdf09-451b-4031-a19a-39ccc8a92f5f",
  "plan_id": null,
  "body": "Placeholder: gave breakfast at 08:30, client walked in the garden.",
  "created_at": "2026-11-25T17:41:52.990208"
}
```

*Any caregiver on the visit may write one. Private: never shown to the family.*

`GET /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **200**:

<!-- capture: 031-caregiver-documentation-list-live.json response.body -->
```json
{
  "items": [
    {
      "id": "622c1ed3-6c9f-4a0a-a08a-3dfb033edf1a",
      "booking_id": "efdbdf09-451b-4031-a19a-39ccc8a92f5f",
      "plan_id": null,
      "body": "Placeholder: gave breakfast at 08:30, client walked in the garden.",
      "created_at": "2026-11-25T17:41:52.990208"
    }
  ],
  "can_add": true,
  "closes_at": null,
  "readable_until": null
}
```

*`can_add` true; `closes_at` and `readable_until` are null while the visit is in progress.*

`GET /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **200**:

<!-- capture: 032-caregiver-documentation-list-after-visit.json response.body -->
```json
{
  "items": [
    {
      "id": "622c1ed3-6c9f-4a0a-a08a-3dfb033edf1a",
      "booking_id": "efdbdf09-451b-4031-a19a-39ccc8a92f5f",
      "plan_id": null,
      "body": "Placeholder: gave breakfast at 08:30, client walked in the garden.",
      "created_at": "2026-11-25T17:41:52.990208"
    }
  ],
  "can_add": true,
  "closes_at": "2026-11-25T19:11:52.999158",
  "readable_until": "2026-11-26T17:11:52.999158"
}
```

*30 minutes after completion: `closes_at` is 2 h after the end, `readable_until` 24 h after it.*

`plan_id` is the client's **active care plan at the time of writing**, or `null` (no plan, or the client cannot be identified). It is informational; the entry belongs to the visit.

### 4.2 Errors the app must handle

`POST /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **409** for this body:

<!-- capture: 033-caregiver-documentation-add-after-window.json request.body -->
```json
{
  "body": "Placeholder: gave breakfast at 08:30, client walked in the garden."
}
```

Response:

<!-- capture: 033-caregiver-documentation-add-after-window.json response.body -->
```json
{
  "detail": {
    "code": "visit_not_active",
    "message": "Notes can be added during a visit and for a short time after it ends."
  }
}
```

*More than 2 h after completion.*

`GET /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **200**:

<!-- capture: 034-caregiver-documentation-list-after-add-window.json response.body -->
```json
{
  "items": [
    {
      "id": "622c1ed3-6c9f-4a0a-a08a-3dfb033edf1a",
      "booking_id": "efdbdf09-451b-4031-a19a-39ccc8a92f5f",
      "plan_id": null,
      "body": "Placeholder: gave breakfast at 08:30, client walked in the garden.",
      "created_at": "2026-11-25T17:41:52.990208"
    }
  ],
  "can_add": false,
  "closes_at": "2026-11-25T17:31:53.003006",
  "readable_until": "2026-11-26T15:31:53.003006"
}
```

*Still readable (24 h), `can_add` false.*

`GET /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **404**:

<!-- capture: 035-other-caregiver-refused.json response.body -->
```json
{
  "detail": {
    "code": "not_found",
    "message": "Visit not found."
  }
}
```

*Someone else's visit: 404.*

`POST /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **422** for this body:

<!-- capture: 036-documentation-validation.json request.body -->
```json
{
  "body": ""
}
```

Response:

<!-- capture: 036-documentation-validation.json response.body -->
```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": [
        "body",
        "body"
      ],
      "msg": "String should have at least 1 character",
      "ctx": {
        "min_length": 1
      }
    }
  ]
}
```

*An empty or whitespace entry, or one over 4000 characters, is the standard 422 (the rejected value is not echoed).*

`GET /api/v1/caregiver/bookings/efdbdf09-451b-4031-a19a-39ccc8a92f5f/documentation` returns **404**:

<!-- capture: 037-caregiver-documentation-list-after-24h.json response.body -->
```json
{
  "detail": {
    "code": "not_found",
    "message": "Visit not found."
  }
}
```

*More than 24 h after the visit ended: 404 `not_found`.*

**Error-shape trap:** on these routes a missing or lost visit is a **coded** `404` (`{"detail": {"code": "not_found", "message": "Visit not found."}}`), while the care-plan routes answer the **plain string** `"Care plan not found"`. Read `detail` as either a string or an object. A suspended caregiver gets the same `404` once the visit is no longer in progress.

### 4.3 State machine and what the screen does

| Visit state | `can_add` | `closes_at` | The app |
|---|---|---|---|
| in progress | `true` | `null` | entry box and list |
| completed, under 2 h | `true` | end + 2 h | entry box, list, line "You can add entries until ..." |
| completed, 2 h to 24 h | `false` | end + 2 h (past) | list only, "You can no longer add ..." |
| completed, over 24 h | | | `404` `not_found`: "This documentation is no longer available." |

No polling is needed. If a save returns `409 visit_not_active` the window ended while the caregiver typed: keep the text on screen, close the form.

## 5. Admin console: Caregiver documentation (read only)

`GET /admin/visit-documentation`, permission **`careplans.read`** (Clinical Reviewer, Super Admin; Support is refused). Query: `booking_id`, `plan_id`, `caregiver_id` (all optional UUIDs), `limit` (1 to 50, default 20), `offset`. **Every call is written to the audit log** as `visit_documentation.view`; the audit row holds the filters and the count, never the text. `no-store` on every response.

`GET /api/v1/admin/visit-documentation` returns **200**:

<!-- capture: 040-admin-documentation-list.json response.body -->
```json
{
  "items": [
    {
      "id": "622c1ed3-6c9f-4a0a-a08a-3dfb033edf1a",
      "booking_id": "efdbdf09-451b-4031-a19a-39ccc8a92f5f",
      "plan_id": null,
      "body": "Placeholder: gave breakfast at 08:30, client walked in the garden.",
      "created_at": "2026-11-25T17:41:52.990208",
      "caregiver": {
        "id": "6b168ee5-6101-42b9-811d-f344fb74d4f5",
        "name": "Placeholder Caregiver Chidi"
      }
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

*Needs `careplans.read`. Every call is written to the audit log (`visit_documentation.view`).*

`GET /api/v1/admin/visit-documentation?b'booking_id=efdbdf09-451b-4031-a19a-39ccc8a92f5f&limit=5'` returns **200**:

<!-- capture: 041-admin-documentation-filtered.json response.body -->
```json
{
  "items": [
    {
      "id": "622c1ed3-6c9f-4a0a-a08a-3dfb033edf1a",
      "booking_id": "efdbdf09-451b-4031-a19a-39ccc8a92f5f",
      "plan_id": null,
      "body": "Placeholder: gave breakfast at 08:30, client walked in the garden.",
      "created_at": "2026-11-25T17:41:52.990208",
      "caregiver": {
        "id": "6b168ee5-6101-42b9-811d-f344fb74d4f5",
        "name": "Placeholder Caregiver Chidi"
      }
    }
  ],
  "total": 1,
  "limit": 5,
  "offset": 0
}
```

`GET /api/v1/admin/visit-documentation` returns **403**:

<!-- capture: 042-admin-documentation-no-permission.json response.body -->
```json
{
  "detail": "Access denied. Missing permission: careplans.read"
}
```

*A Support admin lacks `careplans.read`.*

**Field-nesting trap:** the author is under `caregiver` (`{"id", "name"}`) on the admin list, while the caregiver's own list has no author (every entry is theirs). An erased entry has `body: ""` (account erasure blanks the text and keeps the row).

## 6. Config

No new settings. Both windows are existing ones: `VOICE_NOTE_POST_VISIT_GRACE_MINUTES` (120) and `VOICE_NOTE_EDIT_WINDOW_HOURS` (24). Changing either changes the notes, the voice notes, the care-plan grace and the documentation space together.

## 7. UX consequences the app must surface

1. **A caregiver who is not a nurse now sees a template.** Offer only what `GET /care-plan-templates` returned; do not offer "Create care plan" (blank) when `can_author` is `false`.
2. **The Elderly plan is editable by every caregiver on that client, but not its medicines.** Hide Add medication unless `can_edit_medications`. Any caregiver on the client can change the plan's goals and tasks: show who wrote the plan (`author`).
3. **The 2 hours are a hard stop.** Show the time ("until 19:11"); when it passes, the box goes away and the next call is a `404`. Do not retry a `404` and do not call it an error screen: the visit is simply over.
4. **Own documentation is private. Say so on the screen** ("Only you and the Oakly team can read them. The family does not see them.") and never copy its text into anything the family can see.
5. **Entries cannot be edited or deleted.** Tell the caregiver before they save ("To correct one, add a new entry").
6. **After 24 hours the caregiver loses sight of their own entries** (the admin console keeps them). Say "no longer available", not "deleted".
7. **Templates are still marked `pending` clinical review** until an admin signs them off. The new text is the clinician's, but the system does not know that yet.

## 8. Verification

| Behaviour | Verified | How |
|---|---|---|
| Template text (four rewritten, version 2, Elderly flag) | live | captures 001, 002, 003; migration round trip and text asserted in `test_care_templates_docs_space.py` |
| Non-nurse lists and previews templates; nurse-only template refused | live | 001, 002, 004 |
| `author_scope` on the visit summary | live | 010, 011 |
| Non-nurse creates from Elderly; blank and other templates refused | live | 012, 013 |
| Non-nurse edits an Elderly plan; medicines refused | live | 014, 015 |
| Another caregiver on the same client edits an Elderly plan | harness only | `test_any_caregiver_may_edit_a_plan_made_from_the_elderly_template_but_not_its_medications` (no capture) |
| Nurse grace: summary, plan read, note, edit refused, after-window 404 | live | 020 to 025 |
| Nurse grace: observations list, tick refused, suspended nurse, other nurse, setting honoured | harness only | `test_care_templates_docs_space.py` (no capture) |
| Documentation: add, list (live and after visit), after-window 409, 24 h 404, other caregiver 404, 422 | live | 030 to 037 |
| Documentation: privacy from the patient and colleagues, suspended caregiver, erasure blanks the text | harness only | `test_care_templates_docs_space.py` (no capture) |
| Admin list, filters, `careplans.read` gate | live | 040 to 042; audit rows asserted in the harness |
| The caregiver app screens | jest only | `__tests__/visitDocumentation.test.tsx`, `carePlanHub.test.tsx`, `carePlanNotesProgress.test.tsx`. **Not run on a simulator or phone: visual pass pending.** |
| The admin console page | vitest only | `documentation-list.test.tsx`. Not opened against a running backend in a browser. |
| Push or any provider call | not applicable | none is involved |
