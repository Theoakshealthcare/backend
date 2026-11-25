# FE integration guide: patient app, FallGuard v2.0 (fall-risk screening)

| | |
|---|---|
| **Backend work package** | branch `feature/fallguard-v2-scoring` (migration `0033_fallguard_v2_results`) |
| **Audience** | `apps/patient` (FallGuard questionnaire, result, history). The caregiver module, red-flag events endpoint, PDF, check-ins and the admin console of the handover are **not built yet**. |
| **Base URL** | `http://127.0.0.1:8000` for the local run; every path below starts with `/api/v1` |
| **Source of truth** | Every request and response body, status code and error string below was **copied from a real response** captured by `scripts/capture_patient_fallguard.py` against the running API (patient and caregiver accounts registered through the endpoints, a throwaway Postgres migrated to head). The files are in [`docs/fe-captures/patient-fallguard/`](fe-captures/patient-fallguard/); each example names its file. Anything not exercised live is marked **[not live]**. |
| **Spec** | "Oakly FallGuard v2.0 Developer Handover Package" (10 November 2026). It replaces the 24-question screener. |

## 0. How to read this guide

- **Examples are generated, not typed.** Every JSON block is rendered from the capture named in the comment above it by `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-patient-fallguard.md docs/fe-captures/patient-fallguard` (`--check` fails if a guide is stale).
- **Placeholders.** All data is fake: `example.com` emails, `Placeholder ...` names. IDs and timestamps come from the local run. `age` in a capture depends on the day it was taken, because the server uses its own "today" (Africa/Lagos).
- **The wording is the supplied wording, and it is not signed off.** The question text, answer labels, "explain" sentences and red-flag items R1 to R7 come from the handover package's own `questionnaire_v2_0.json`; the recommendation sentences and messages are the PDF's default English. The clinical lead's sign-off (Appendix E of the PDF) is still blank, so every string carries `wording_status: PENDING_CLINICAL_SIGN_OFF`. **Nothing here may go live before that sign-off** (section 8).
- Section 9 is the table of what is verified live and what is not.

## 1. What the feature is, in the app's terms

A person aged 60 or over answers a short questionnaire in three parts (A: falls history; B: risk factors; C: living situation and extra safety questions) and the server returns a **risk tier** (LOW, INTERMEDIATE or HIGH), the reasons, and a prioritised list of actions. People under 60 get no score and no tier. A separate **urgent** result tells the app to show the emergency screen.

The rules that shape every screen:

- **The server alone decides the tier.** The app never computes or adjusts it (it only detects red flags, section 5). Show `result.final_tier` exactly as returned.
- **Urgent never changes the tier and is never hidden by it.** `result.urgent` is its own switch.
- **An unknown answer never counts as "fine".** A Part B item the person cannot or will not answer scores 1, not 0. More than 3 unknown items makes the result **provisional**.
- **A result is immutable.** A changed answer is a new submission; the old result stays in the history.

## 2. Conventions

| Topic | What is true | Evidence |
|---|---|---|
| Auth | `Authorization: Bearer <access_token>` of a **patient** account. | all |
| No token | `401` `{"detail": "Not authenticated"}` | 004, 024 |
| A caregiver token | `403` `{"detail": "Access denied. Patient account required."}`: `detail` is a **string**. | 003, 023 |
| Error shape of `submit` for a refused submission | `422`/`429` `{"detail": {"code": "...", "message": "...", "question_id": "...", "fields": {...}}}`: **an object**. `code` is stable: switch on it. `question_id` and `fields` are present only when they apply. | 015 to 021, 026 |
| Malformed body | `422` with `detail` as a **list** (FastAPI's own format). Same status, different shape: check `Array.isArray(detail)`. | 022 |
| Timestamps | ISO 8601 UTC without `Z`, e.g. `2026-11-25T17:27:56.256322`. | 006 |

### 2.1 Field-nesting traps

- The tier fields are **inside `result`**, not at the top of the response: `body.result.final_tier`, not `body.final_tier`. The top level has `total_score` (the Part B total, **null** under 60), the three version strings, `result`, `recommendations`, and the older `ai_*` fields.
- `total_score` (top level) and `result.score` are the same number. Prefer `result`.
- `answers[].score` is the item's Part B points, and **null** for Part A and C items. `answers[].id` is always **null** for a v2.0 assessment (it exists only on old 24-question rows).
- `answers[].answer` is always a **string**, even for numbers (`"1"`) and lists (`"DIABETES,ARTHRITIS"`). The request sends real numbers and arrays.
- `questions[].id` is now an **item id** (`"A1"`), not a UUID. It is still a string.
- When `result.eligible` is false (under 60) `result` has **fewer keys**: `gate_tier`, `score_tier`, `final_tier` and `score` are `null`, `referral_message_key` is `"UNDER_60_REFERRAL"`, and `gate_reasons`, `incomplete`, `provisional`, `unknown_items`, `part_b_points`, `top_factors`, `consequence_flag` and `pharmacist_review_flag` are **absent**, not null (section 3.3). Check `eligible` before reading them.

### 2.2 The questions: `GET /api/v1/fallguard/questions`

The 34 items in order (A1 to A7, B1 to B19, C1 to C8). The older fields (`id`, `question`, `options[].text`, `options[].score`, `order`, `is_active`) keep their names; the rest is additive. **Send `options[].code` back as the answer** (not `text`). `score` is the points of a Part B choice and is **null** for Part A and Part C choices; do not use it to compute a result.

<!-- capture: 001-p-questions.json response.body first=2 -->
```json
[
  {
    "id": "A1",
    "question": "In the last 12 months, have you fallen (landed on the ground or a lower level by accident)?",
    "options": [
      {
        "text": "No",
        "score": null,
        "code": 0
      },
      {
        "text": "Yes, once",
        "score": null,
        "code": 1
      },
      {
        "text": "Yes, 2 or more times",
        "score": null,
        "code": 2
      }
    ],
    "order": 1,
    "is_active": true,
    "created_at": null,
    "updated_at": null,
    "part": "A",
    "type": "single",
    "required": true,
    "allows_unknown": false,
    "show_if": null,
    "icon": "fall",
    "explain": null,
    "exclusive_codes": null,
    "scoring": null,
    "fields": null,
    "wording_status": "PENDING_CLINICAL_SIGN_OFF"
  },
  {
    "id": "A2",
    "question": "Did any fall cause an injury that needed medical care?",
    "options": [
      {
        "text": "No",
        "score": null,
        "code": 0
      },
      {
        "text": "Yes",
        "score": null,
        "code": 1
      }
    ],
    "order": 2,
    "is_active": true,
    "created_at": null,
    "updated_at": null,
    "part": "A",
    "type": "single",
    "required": true,
    "allows_unknown": false,
    "show_if": {
      "item": "A1",
      "op": ">=",
      "value": 1
    },
    "icon": "injury",
    "explain": null,
    "exclusive_codes": null,
    "scoring": null,
    "fields": null,
    "wording_status": "PENDING_CLINICAL_SIGN_OFF"
  }
]
```

Item types: `single` (send the code), `multi` (send a list of codes; `exclusive_codes` such as `NONE` and `NOT_SURE` cannot be combined with another code, an empty list is invalid, so the person must tick "None of these"), `derived` (B1, the age: **not asked**, never send it), `text_list` (C7, optional free text, a list of strings or one string), `demographics` (C8, optional, `{"sex": ..., "education": ...}` with the codes in `fields`). `explain` is the sentence the result screen shows when the item is a top factor; `icon` is the questionnaire's icon key. `show_if` means: skip the item when the condition is false (A2, A3 and A7 only when A1 is 1 or more; C3 only when B2 is 1). A hidden item counts as 0 and an answer sent for it is ignored.

`allows_unknown` is true for Part B items except B1: offer "I don't know" and "Skip, ask my nurse" as secondary buttons and send the string `"UNKNOWN"` for both.

Part A items are required (A1, A4, A5, A6 always; A2, A3, A7 when A1 is 1 or more). Part B and C items may be left out: a Part B item left out is treated as unknown.

### 2.3 The config: `GET /api/v1/fallguard/config?lang=en`

Everything needed to render the flow: versions, items, the seven red-flag items, the fixed messages (Urgent text, referral text, disclaimer, labels), the welcome text and the emergency number. Only English exists; `lang` is accepted and every string is English (the app falls back per string, as the PDF says).

<!-- capture: 002-p-config.json response.body keys=questionnaire_version,algorithm_version,content_version,language,wording_status,content_sign_off_status,emergency_number,assessment_minutes,min_age,red_flags -->
```json
{
  "questionnaire_version": "2.0.0",
  "algorithm_version": "2.0.0",
  "content_version": "2.0.0",
  "language": "en",
  "wording_status": "PENDING_CLINICAL_SIGN_OFF",
  "content_sign_off_status": "PENDING_CLINICAL_LEAD_SIGN_OFF",
  "emergency_number": "112",
  "assessment_minutes": 8,
  "min_age": 60,
  "red_flags": [
    {
      "id": "R1",
      "text": "Did you hit your head?",
      "rule": "URGENT unless C5 == 0 (then prompt attention)"
    },
    {
      "id": "R2",
      "text": "Since the fall, do you have a worsening headache, repeated vomiting, or unusual sleepiness or confusion?",
      "rule": "URGENT"
    },
    {
      "id": "R3",
      "text": "Do you have severe pain, cannot put weight on a leg, or see a deformed arm or leg?",
      "rule": "URGENT"
    },
    {
      "id": "R4",
      "text": "Do you have new weakness or numbness on one side, a drooping face, or trouble speaking?",
      "rule": "URGENT"
    },
    {
      "id": "R5",
      "text": "Do you have chest pain, a racing or irregular heartbeat, or severe breathlessness?",
      "rule": "URGENT"
    },
    {
      "id": "R6",
      "text": "Did you faint or black out?",
      "rule": "URGENT"
    },
    {
      "id": "R7",
      "text": "Are you unable to get up, and is nobody with you?",
      "rule": "URGENT"
    }
  ]
}
```

<!-- capture: 002-p-config.json response.body path=messages -->
```json
{
  "result.low": "Lower risk right now. Your answers suggest a lower risk of falling right now. Falls can still happen, so here is how to stay steady and protect yourself.",
  "result.intermediate": "Some factors raise your risk. You have some factors that raise your risk of falling. The steps below can reduce it.",
  "result.high": "Several factors raise your risk. You have several factors that raise your risk of falling. A clinical assessment is recommended.",
  "provisional.label": "Provisional result: some answers were missing. A nurse can help you complete it.",
  "overridden.label": "Reviewed by your clinician",
  "prompt.message": "Please contact a nurse or doctor in the next few days.",
  "urgent.message": "Please get medical help now. Call the emergency number or go to the nearest emergency department. Do not drive yourself. If someone is with you, ask them to stay with you.",
  "error.safety": "We could not calculate your result. If you have fallen recently or feel unwell, get medical help now. Your answers are saved so you can try again.",
  "referral.under60": "This screener is designed for people aged 60 and over. If you have fallen or feel unsteady, please speak to a nurse or doctor.",
  "disclaimer": "This screener helps you understand your risk. It cannot examine you or replace advice from a doctor or nurse. If you feel unwell, get medical help.",
  "carer.alert": "Please check on {name} now.",
  "notify.checkin": "You have a FallGuard check-in.",
  "notify.reassess": "It is time to repeat your FallGuard assessment."
}
```

### 2.4 What is still not final (do not ship until it is)

- **Every string is unsigned.** Appendix E of the PDF (the clinical lead's sign-off of the gate rules, red-flag items and wording, the Part B scale and cut-offs, the recommendation wording, the service targets and the welcome text) is blank. `wording_status` is `PENDING_CLINICAL_SIGN_OFF` on every item and in the config.
- **There is no sentence for a gate reason.** `result.gate_reasons` holds codes (`GATE_I_ONE_FALL`, `GATE_H_FAINTING`, ...). The package supplies "explain" strings for Part B items only, so the app needs a sentence per gate code from the clinical lead. Treat the code as an opaque key and never show it.
- **There is no per-state emergency number list yet:** `emergency_number` is the default `112` everywhere.
- The Appendix A example in the PDF lists `C9` (completer role, language, mode) as an "auto" item; the supplied questionnaire has no C9, so the API has none. The app records who answered and the mode itself when the assessment record exists (a later slice).

## 3. Submit: `POST /api/v1/fallguard/submit`

Body: `{"answers": [{"question_id": "A1", "answer": 1}, ...], "date_of_birth": "1946-05-20"}`.

- `answer` is a number for single items, a list of strings for multi items and C7, the string `"UNKNOWN"` for an unknown Part B item, and `true`/`false` for a red-flag item (R1 to R7: send only the ones that were shown; a red-flag item left out counts as not yes). A red-flag answer must be a real JSON boolean: `"yes"` or `1` is a `422` (020), because the scoring library only counts `true`.
- `date_of_birth` (ISO date) is used for the age. If it is left out the date of birth on the patient's profile is used; if neither exists the answer is `422` `fallguard_date_of_birth_required` (022). **Age is worked out on the day of the submission in Africa/Lagos, by the server.**
- `score` on an answer is accepted and ignored (014).
- Send the whole assessment at once. There is no server-side draft in this release: **keep the draft on the device** and submit at the end.

### 3.1 A normal result (the PDF's own example, Appendix A)

<!-- capture: 006-p-submit-appendix-a.json request.body path=answers first=3 -->
```json
[
  {
    "question_id": "A1",
    "answer": 1
  },
  {
    "question_id": "A2",
    "answer": 0
  },
  {
    "question_id": "A3",
    "answer": 0
  }
]
```

<!-- capture: 006-p-submit-appendix-a.json response.body keys=id,patient_id,total_score,ai_summary,ai_recommendations,created_at,questionnaire_version,algorithm_version,content_version,result -->
```json
{
  "id": "97b5610b-2fa3-46ec-851b-f3642d474d33",
  "patient_id": "c5470c8e-7b9e-4aed-b63b-b83a1beb1f68",
  "total_score": 18,
  "ai_summary": null,
  "ai_recommendations": null,
  "created_at": "2026-11-25T21:23:32.052429",
  "questionnaire_version": "2.0.0",
  "algorithm_version": "2.0.0",
  "content_version": "2.0.0",
  "result": {
    "questionnaire_version": "2.0.0",
    "algorithm_version": "2.0.0",
    "age": 80,
    "urgent": false,
    "red_flags_triggered": [],
    "eligible": true,
    "gate_tier": "INTERMEDIATE",
    "gate_reasons": [
      "GATE_I_ONE_FALL",
      "GATE_I_UNSTEADY",
      "GATE_I_WORRY"
    ],
    "score": 18,
    "score_tier": "HIGH",
    "final_tier": "HIGH",
    "incomplete": false,
    "provisional": false,
    "unknown_items": [],
    "part_b_points": {
      "B1": 2,
      "B2": 1,
      "B3": 1,
      "B4": 1,
      "B5": 2,
      "B6": 1,
      "B7": 2,
      "B8": 1,
      "B9": 1,
      "B10": 0,
      "B11": 1,
      "B12": 0,
      "B13": 1,
      "B14": 1,
      "B15": 1,
      "B16": 0,
      "B17": 1,
      "B18": 1,
      "B19": 0
    },
    "top_factors": [
      "B5",
      "B7",
      "B2"
    ],
    "prompt_attention": false,
    "consequence_flag": true,
    "pharmacist_review_flag": false
  }
}
```

What the fields mean for the screen:

| Field | Use |
|---|---|
| `result.final_tier` | The tier to show: `LOW`, `INTERMEDIATE` or `HIGH`. Always with the word, never colour alone. |
| `result.gate_tier`, `result.score_tier` | The two inputs. `final_tier` is the higher of the two; it is never below `gate_tier`. Show only `final_tier`. |
| `result.gate_reasons` | The rules that raised the gate tier, e.g. `GATE_I_ONE_FALL`. Show a plain sentence for each, **before** the factor sentences. The package supplies no sentences for gate reasons (2.4). |
| `result.top_factors` | Up to 3 Part B item ids (B1 never appears) behind the score, highest points first. Turn them into sentences with the item's `explain` string from the questions list. An item id here can be an **unknown** item, so check `unknown_items`. |
| `result.score`, `result.part_b_points` | The Part B total and per-item points. Do not show the total as a "percentage" or compute a tier from it. |
| `result.incomplete` and `result.provisional` | Always equal. True when more than 3 Part B items were unknown or skipped: show the provisional label and offer nurse-assisted completion. The tier is still shown. |
| `result.unknown_items` | The Part B items that scored 1 because they were unknown, skipped or left out. |
| `result.urgent` | Show the Urgent screen (section 5). |
| `result.prompt_attention` | Show the banner `messages["prompt.message"]` at the top of the result with a one-tap booking button. |
| `result.consequence_flag` | Show the "If you fall" content and the bone-health prompt (its recommendation, REC-FALLPLAN, is already in the list). |
| `result.pharmacist_review_flag` | Offer a pharmacist review. |
| `recommendations.top` / `.all` | The actions. Show `top` (3) first and "See all actions" for `all`. Sorted by priority. Already de-duplicated. |
| `questionnaire_version`, `algorithm_version`, `content_version` | Show in the history and the PDF footer. |

### 3.2 The recommendations

<!-- capture: 006-p-submit-appendix-a.json response.body path=recommendations.top -->
```json
[
  {
    "id": "REC-CLINICAL",
    "priority": 2,
    "text": "A clinical assessment is recommended. A nurse can visit you at home to look at your balance, medicines, eyesight, home and more."
  },
  {
    "id": "REC-EXERCISE",
    "priority": 3,
    "text": "Strength and balance exercise is the best-proven way to reduce falls. Join a structured programme such as a supervised class, Tai Chi or a physiotherapist-led plan."
  },
  {
    "id": "REC-BOOK-NURSE",
    "priority": 4,
    "text": "Book a nurse home visit to look at your risk in more detail."
  }
]
```

The list is rule based (the PDF's catalogue). **It is not AI text.** Show every entry that applies; the server has already ordered them. `REC-LOW` is only ever sent with a LOW tier.

### 3.3 Other results

A fainting answer raises the tier by itself (gate HIGH, score LOW, final HIGH) and sets `prompt_attention`:

<!-- capture: 008-p-submit-gate-raises-tier.json response.body path=result keys=gate_tier,gate_reasons,score,score_tier,final_tier,top_factors,prompt_attention -->
```json
{
  "gate_tier": "HIGH",
  "gate_reasons": [
    "GATE_H_FAINTING"
  ],
  "score": 1,
  "score_tier": "LOW",
  "final_tier": "HIGH",
  "top_factors": [],
  "prompt_attention": true
}
```

Part A only (every Part B item unknown): provisional, and the unknown items score 1 so the score is not low:

<!-- capture: 009-p-submit-incomplete.json response.body path=result keys=gate_tier,score,score_tier,final_tier,incomplete,provisional,unknown_items,top_factors -->
```json
{
  "gate_tier": "LOW",
  "score": 19,
  "score_tier": "HIGH",
  "final_tier": "HIGH",
  "incomplete": true,
  "provisional": true,
  "unknown_items": [
    "B2",
    "B3",
    "B4",
    "B5",
    "B6",
    "B7",
    "B8",
    "B9",
    "B10",
    "B11",
    "B12",
    "B13",
    "B14",
    "B15",
    "B16",
    "B17",
    "B18",
    "B19"
  ],
  "top_factors": [
    "B2",
    "B3",
    "B4"
  ]
}
```

Under 60 (no score, no tier, no recommendations; note the shorter `result`: see 2.1):

<!-- capture: 010-p-submit-under-60.json response.body keys=total_score,result,recommendations -->
```json
{
  "total_score": null,
  "result": {
    "questionnaire_version": "2.0.0",
    "algorithm_version": "2.0.0",
    "age": 36,
    "urgent": false,
    "red_flags_triggered": [],
    "eligible": false,
    "final_tier": null,
    "gate_tier": null,
    "score_tier": null,
    "score": null,
    "referral_message_key": "UNDER_60_REFERRAL",
    "prompt_attention": false
  },
  "recommendations": {
    "top": [],
    "all": []
  }
}
```

Show `messages["referral.under60"]` and the help button. Do not show a tier.

### 3.4 Errors of `submit`

| Status and `code` | When | Evidence |
|---|---|---|
| `422` `fallguard_no_answers` | empty list | 015 |
| `422` `fallguard_duplicate_question` | the same item twice | 016 |
| `422` `fallguard_unknown_question` | an item id that is not in the questionnaire (a v1 UUID lands here), B1 (derived), or a red-flag id other than R1 to R7 | 017 |
| `422` `INVALID_ANSWER` | an answer that is not one of the item's codes; a wrong type (the library's own code); a red-flag answer that is not a boolean; a Part C or multi-select answer of the wrong shape | 018, 020, 021 |
| `422` `EXCLUSIVE_ANSWER` | `NONE` or `NOT_SURE` combined with another code in a multi list **[not live]** (library code; asserted in `app/tests/test_fallguard_rules.py`) | none |
| `422` `MISSING_REQUIRED` | a required Part A item is missing; **only the first missing item** is named, in `question_id` and `fields` | 019 |
| `422` `fallguard_date_of_birth_required` | no date of birth in the body or the profile | 022 |
| `422` `fallguard_invalid_date_of_birth` | a date of birth in the future | 023 |
| `422` `INVALID_DATE` | a date of birth that is not a calendar date **[not live]** (library code) | none |
| `429` `fallguard_rate_limited` | the 11th submission within an hour | 028 |
| `503` `SCORING_UNAVAILABLE` | scoring or storing failed: **no tier was stored or returned.** **[not live]** (a fault cannot be triggered in the running app; shape asserted in `app/tests/test_fallguard_submit.py`) | none |

<!-- capture: 019-p-submit-missing-required.json response.body -->
```json
{
  "detail": {
    "code": "MISSING_REQUIRED",
    "message": "Answer every required question.",
    "question_id": "A5",
    "fields": {
      "A5": "MISSING_REQUIRED"
    }
  }
}
```

<!-- capture: 018-p-submit-invalid-option.json response.body -->
```json
{
  "detail": {
    "code": "INVALID_ANSWER",
    "message": "That answer is not one of the choices for the question.",
    "question_id": "A1",
    "fields": {
      "A1": "INVALID_ANSWER"
    }
  }
}
```

<!-- capture: 017-p-submit-unknown-question.json response.body -->
```json
{
  "detail": {
    "code": "fallguard_unknown_question",
    "message": "One of the questions is not part of the assessment.",
    "question_id": "fa1b5563-41d4-43cd-a784-690cc9a9d622"
  }
}
```

<!-- capture: 016-p-submit-duplicate-question.json response.body -->
```json
{
  "detail": {
    "code": "fallguard_duplicate_question",
    "message": "Each question can only be answered once.",
    "question_id": "A1"
  }
}
```

<!-- capture: 015-p-submit-empty.json response.body -->
```json
{
  "detail": {
    "code": "fallguard_no_answers",
    "message": "Answer at least one question."
  }
}
```

<!-- capture: 022-p-submit-no-date-of-birth.json response.body -->
```json
{
  "detail": {
    "code": "fallguard_date_of_birth_required",
    "message": "Date of birth is needed to work out the age.",
    "fields": {
      "date_of_birth": "required"
    }
  }
}
```

<!-- capture: 023-p-submit-future-date-of-birth.json response.body -->
```json
{
  "detail": {
    "code": "fallguard_invalid_date_of_birth",
    "message": "Date of birth is in the future.",
    "fields": {
      "date_of_birth": "future"
    }
  }
}
```

<!-- capture: 028-p-submit-rate-limited.json response.body -->
```json
{
  "detail": {
    "code": "fallguard_rate_limited",
    "message": "Too many assessments in the last hour. Please try again later."
  }
}
```

<!-- capture: 024-p-submit-bad-body.json response.body -->
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "body",
        "answers",
        0,
        "question_id"
      ],
      "msg": "Field required",
      "input": {
        "answer": 0
      }
    }
  ]
}
```

**A `503` `SCORING_UNAVAILABLE` must show the safety screen**, never a tier: `messages["error.safety"]` plus the emergency details, and keep the answers on the device for a retry. Its `detail.message` is that same text.

Not logged in, or a caregiver token:

<!-- capture: 025-c-submit-as-caregiver.json response.body -->
```json
{
  "detail": "Access denied. Patient account required."
}
```

<!-- capture: 026-none-submit-no-token.json response.body -->
```json
{
  "detail": "Not authenticated"
}
```

## 4. History: `GET /api/v1/fallguard/history`

The patient's own assessments, newest first, in the same shape as a submit response. A row from the old 24-question screener (if any exists) has `result`, `recommendations` and the three versions set to **null** and its `answers[].id` set: show those as "earlier check" without a tier.

<!-- capture: 005-p-history-empty.json response.body -->
```json
[]
```

<!-- capture: 027-p-history.json response.body path=0 keys=id,patient_id,total_score,created_at,questionnaire_version,algorithm_version,content_version -->
```json
{
  "id": "22929f4e-4d12-4b7d-b367-35d6615ba8c3",
  "patient_id": "c5470c8e-7b9e-4aed-b63b-b83a1beb1f68",
  "total_score": 1,
  "created_at": "2026-11-25T21:23:32.086540",
  "questionnaire_version": "2.0.0",
  "algorithm_version": "2.0.0",
  "content_version": "2.0.0"
}
```

## 5. Red flags and the Urgent screen

The red-flag items R1 to R7 are sent in the same `answers` list as `true` or `false`. The server decides `urgent`:

- any of R2 to R7 answered yes: `urgent` is true;
- R1 yes and C5 (blood thinners) is 1, 9 (not sure) or **not answered**: `urgent` is true (the cautious default);
- R1 yes and C5 is 0: not urgent, but `prompt_attention` is true.

`result.red_flags_triggered` lists **every** red-flag item answered yes, urgent or not: in the third case it is `["R1"]` while `urgent` is false. **Decide the Urgent screen from `urgent`, never from the list being non-empty.**

<!-- capture: 011-p-submit-urgent.json response.body path=result keys=urgent,red_flags_triggered,final_tier,score -->
```json
{
  "urgent": true,
  "red_flags_triggered": [
    "R3"
  ],
  "final_tier": "LOW",
  "score": 1
}
```

<!-- capture: 012-p-submit-r1-no-blood-thinner.json response.body path=result keys=urgent,red_flags_triggered,prompt_attention,final_tier -->
```json
{
  "urgent": false,
  "red_flags_triggered": [
    "R1"
  ],
  "prompt_attention": true,
  "final_tier": "LOW"
}
```

<!-- capture: 013-p-submit-r1-blood-thinner.json response.body path=result keys=urgent,red_flags_triggered,prompt_attention,final_tier -->
```json
{
  "urgent": true,
  "red_flags_triggered": [
    "R1"
  ],
  "prompt_attention": false,
  "final_tier": "LOW"
}
```

**The Urgent screen is not decided by this endpoint alone.** The handover requires the red-flag check, the Urgent screen and the "I need help now" button to work **with no network** (the app bundles them), so the app must evaluate R1 to R7 itself and show the Urgent screen immediately, then treat `result.urgent` from the server as confirmation. The same logic (the three bullets above) must be implemented in the app and kept in step with the server. There is no `red-flag-events` endpoint yet, so there is nowhere to send the offline event.

## 6. Admin list (read only)

`GET /api/v1/fallguard/admin/questions` returns the same 34 items. The old admin editor is retired: creating, editing or deleting a question is `410` `fallguard_questionnaire_is_versioned`, because the questionnaire changes only with a release (the PDF forbids hot edits). The admin console must remove its question editor.

<!-- capture: 030-a-admin-create-question-retired.json response.body -->
```json
{
  "detail": {
    "code": "fallguard_questionnaire_is_versioned",
    "message": "The FallGuard questionnaire is versioned and changes only with a release."
  }
}
```

## 7. UX consequences the app must surface

1. **Show the server's tier and nothing else.** Never compute, round or "soften" it, and never show a tier when `eligible` is false.
2. **Urgent is separate from the tier.** A LOW result can be urgent. Show the Urgent screen first when `urgent` is true, with the emergency number from the config and no booking or marketing button on it.
3. **Provisional is not final.** When `provisional` is true, say so (`messages["provisional.label"]`) and offer a nurse-assisted completion. The tier shown may be higher than the person expects because unknown answers score as 1; that is deliberate.
4. **`prompt_attention` is a banner, not a tier.** Show `messages["prompt.message"]` at the top with a booking button. The booking link may carry only the service type and the subject: **no health detail in a URL, a notification or a log.**
5. **Never imply certainty.** The PDF bans "safe" as a verdict, "no risk", "you will not fall", "clinically validated", "predicts" and "diagnosis" in any string. Use the server's messages and the disclaimer text as given.
6. **The recommendation text is clinical wording awaiting sign-off.** Do not edit it; it changes only through the clinical lead.
7. **A 429 means wait, not retry now.** The limit is 10 submissions per hour per patient.
8. **A double tap on Submit creates two assessments** (the PDF's `Idempotency-Key` header is not implemented yet); disable the button while the request is in flight.
9. **The AI summary is off.** `ai_summary` is `null` and `ai_recommendations` is `null`. If the owner switches it on later, it comes with `ai_review_status: "PENDING_CLINICIAN_REVIEW"`: show it only after a clinician has reviewed the wording. **[not live]**

## 8. Not ready to launch: what is missing from the PDF

The clinical lead's sign-off (Appendix E is blank), a sentence for each gate-reason code, and the per-state emergency numbers are still missing; the wording, R1 to R7 and the explain strings were supplied with the handover package but are unsigned. The full list is in the SOP, under "Open questions for the clinician" (`docs/sop/2026-11-25-fallguard-v2-backend.md`). The app screens should be built against the shapes above, with those strings coming from the server so they can change without an app release.

## 9. Verification table

| Behaviour | Verified live? | Evidence / reason |
|---|---|---|
| Questions: 34 items in order (the supplied wording); 403 for a caregiver; 401 without a token | **Yes** | 001, 003, 004 |
| Config: versions, real red-flag items R1 to R7, messages, emergency number | **Yes** | 002 |
| Appendix A example gives the PDF's tier, score 18, gate reasons, top factors | **Yes** (age depends on the day) | 006 |
| LOW result; gate raises the tier; provisional; under 60 | **Yes** | 007 to 010 |
| Urgent; R1 with and without blood thinners | **Yes** | 011 to 013 |
| `score` ignored | **Yes** | 014 |
| 422 for empty, duplicate, unknown, invalid, missing required, red flag not boolean, bad Part C, no date of birth, future date of birth; malformed body | **Yes** | 015 to 024 |
| 403 and 401 on submit | **Yes** | 025, 026 |
| History empty, then newest first | **Yes** | 005, 027 |
| 429 on the 11th submission in an hour | **Yes** | 028 |
| Admin list readable; admin create, update and delete are 410 | **Yes** | 029 to 032 |
| `503 SCORING_UNAVAILABLE`: nothing stored, no tier returned | **Not live** | unit test (`app/tests/test_fallguard_submit.py`); a fault cannot be triggered in the running app |
| `EXCLUSIVE_ANSWER`, `INVALID_DATE` | **Not live** | `app/tests/test_fallguard_rules.py` |
| Immutability (a stored result cannot be updated) | **Not live over HTTP** | database trigger tested in `app/tests/test_fallguard_submit.py` and the migration test |
| Date of birth taken from the patient profile when the body has none | **Not live** | unit test; no profile date-of-birth call in the capture |
| Every rule's edge cases (the package's TV1 to TV17, 10,000-case properties, parity) | **Not live** | `app/tests/test_fallguard_scoring.py` (the package's own suite) and `test_fallguard_rules.py` |
| The optional AI summary | **No** | off by default; no Gemini key in the capture stack |
| The patient app screens against this | **No** | not part of this slice |
