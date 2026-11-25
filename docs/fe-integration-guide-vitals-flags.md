# FE integration guide: vitals flags (Normal / High / Low / Critical)

| | |
|---|---|
| **Backend work package** | branch `feature/vitals-flags`, **no migration**; [SOP](sop/2026-11-25-vitals-flags.md) |
| **Audience** | `apps/caregiver` (Vitals tab, a vital's page, history, Client Profile) and `apps/patient` (Vitals home, monitoring page, history). The admin console shows no vitals today. |
| **Base URL** | local test harness; every path starts with `/api/v1` |
| **Source of truth** | Every body below is **rendered from a capture** in [`docs/fe-captures/vitals-flags/`](fe-captures/vitals-flags/) written by `app/tests/test_capture_vitals_flags.py` (the real app and a real Postgres). Examples are regenerated with `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-vitals-flags.md docs/fe-captures/vitals-flags`. All data is fake. |

## 1. What this is, in the app's terms

Every reading the vitals API returns now carries a **flag**: the clinician-approved rating **Normal, High, Low or Critical**, worked out by the server. **The apps never rate a reading themselves**: draw what `flag` says, and draw nothing when it is `null`. Nothing was removed or renamed; `flag` and `glucose_context` are new fields.

| Where it appears | Field | Notes |
|---|---|---|
| `POST /vitals/`, `PUT /vitals/{id}`, `GET /vitals/{patient_id}` | `flag`, `glucose_context` on each reading | top level of the reading |
| `GET /caregiver/bookings/{booking_id}/client` | `latest_vitals[].flag`, `latest_vitals[].glucose_context` | same shape, inside each `latest_vitals` item |
| `GET /vitals/{patient_id}/download` (CSV) | unchanged | no flag column |

Weight and sleep are **never flagged** (`flag` is `null`). A `null` flag also means "the server could not rate this" (a legacy row whose value is not a number, a type the server does not know). **No badge is not Normal.**

## 2. The `flag` object

```
flag: { status, label, reason, emergency, message } | null
```

| Field | Type | Meaning |
|---|---|---|
| `status` | `"normal" \| "high" \| "low" \| "critical"` | Drive colour and icon from this. Treat an unknown value as "no flag". |
| `label` | string | The word to show: `Normal`, `High`, `Low`, `Critical`. |
| `reason` | string \| **null** | One sentence for a detail view or a tooltip; `null` when normal. It names the number that was crossed, never the patient's condition. |
| `emergency` | boolean | `true` only for `critical`. |
| `message` | string \| **null** | `"This may be an emergency. Call 112 now."` when `emergency`, else `null`. **Show it, prominently, whenever it is set.** |

**Present-but-null, not absent:** `flag` and `glucose_context` are always in the JSON. `flag` is `null` for sleep; `glucose_context` is `null` for every non-glucose reading and for glucose with no label.

### Thresholds (the clinical lead's; for your reference only, never re-implement them in the app)

| Vital | Normal | High | Low | Critical |
|---|---|---|---|---|
| Heart rate | 60 to 100 bpm | above 100 | below 60 | below 50 |
| Blood pressure | 120/80 to 140/90 | above 140/90 (above 130/80 for diabetes or kidney disease) | below 100/60 | top number below 90 |
| Temperature | 36.5 to 37.5 °C (a °F reading is converted) | above 37.5 | below 36.5 | none |
| Respiratory rate | 12 to 20 per minute | above 20 | below 12 | none |
| Blood glucose | see below | fasting 126 mg/dL (7.0 mmol/L) or more; random 200 mg/dL (11.1 mmol/L) or more | below 70 mg/dL (3.9 mmol/L) | none |
| Sleep, weight | not flagged | | | |

## 3. Examples (all captured)

The same endpoints you already call; the new part is `flag` and `glucose_context`.

**A high heart rate** (`POST /vitals/`, patient token):

<!-- capture: 001-create-heart-rate-high.json response.body -->
```json
{
  "vital_type": "heart_rate",
  "value": "110",
  "unit": "bpm",
  "source": "manual",
  "recorded_at": "2026-11-25T17:25:09.276034",
  "notes": null,
  "id": "75839d54-105f-4645-b957-198f5fb5fb5b",
  "patient_id": "59e25e8c-81cc-4055-afd3-1aae1dfa4364",
  "recorded_by_user_id": "0ccb19bc-b91e-4ea7-aa91-fe21d55eee12",
  "flag": {
    "status": "high",
    "label": "High",
    "reason": "Heart rate above 100 bpm. Tell the family or the patient's doctor for a medical review.",
    "emergency": false,
    "message": null
  },
  "glucose_context": null
}
```

**A critical reading** carries the emergency fields:

<!-- capture: 002-create-heart-rate-critical.json response.body path=flag -->
```json
{
  "status": "critical",
  "label": "Critical",
  "reason": "Heart rate below 50 bpm. Contact the family and arrange an ambulance.",
  "emergency": true,
  "message": "This may be an emergency. Call 112 now."
}
```

**Blood pressure: normal, low, critical:**

<!-- capture: 003-create-blood-pressure-normal.json response.body path=flag -->
```json
{
  "status": "normal",
  "label": "Normal",
  "reason": null,
  "emergency": false,
  "message": null
}
```

<!-- capture: 004-create-blood-pressure-low.json response.body path=flag -->
```json
{
  "status": "low",
  "label": "Low",
  "reason": "Blood pressure below 100/60 mmHg.",
  "emergency": false,
  "message": null
}
```

<!-- capture: 005-create-blood-pressure-critical.json response.body path=flag -->
```json
{
  "status": "critical",
  "label": "Critical",
  "reason": "Top number below 90 mmHg. Contact the family and arrange an ambulance.",
  "emergency": true,
  "message": "This may be an emergency. Call 112 now."
}
```

**A temperature entered in °F is judged in °C** (100.4 °F is 38.0 °C):

<!-- capture: 006-create-temperature-high-fahrenheit.json response.body path=flag -->
```json
{
  "status": "high",
  "label": "High",
  "reason": "Temperature above 37.5 °C.",
  "emergency": false,
  "message": null
}
```

**Sleep is never flagged** (`flag` is `null`, present):

<!-- capture: 010-create-sleep-no-flag.json response.body -->
```json
{
  "vital_type": "sleep_duration",
  "value": "5",
  "unit": "hrs",
  "source": "manual",
  "recorded_at": "2026-11-25T17:25:09.319093",
  "notes": "22:30-03:30",
  "id": "4ef2c658-11e5-43d6-838d-c67962fcab2a",
  "patient_id": "59e25e8c-81cc-4055-afd3-1aae1dfa4364",
  "recorded_by_user_id": "0ccb19bc-b91e-4ea7-aa91-fe21d55eee12",
  "flag": null,
  "glucose_context": null
}
```

## 4. Blood glucose: the label changes the verdict

The apps already store the label as the reading's `notes`: `RBS` (Random), `FBS` (Fasting), `PP` (After meal). The server reads it and returns it as `glucose_context` (`random`, `fasting`, `after_meal`), and `notes` stays exactly what was saved. **The same 130 mg/dL is High when fasting and Normal when random:**

<!-- capture: 007-create-glucose-fasting-high.json response.body keys=value,unit,notes,glucose_context,flag -->
```json
{
  "value": "130",
  "unit": "mg/dL",
  "notes": "FBS",
  "glucose_context": "fasting",
  "flag": {
    "status": "high",
    "label": "High",
    "reason": "Blood glucose 126 mg/dL or more (fasting). Tell the patient's doctor or book a review, and inform the family.",
    "emergency": false,
    "message": null
  }
}
```

<!-- capture: 008-create-glucose-random-same-value-normal.json response.body keys=value,unit,notes,glucose_context,flag -->
```json
{
  "value": "130",
  "unit": "mg/dL",
  "notes": "RBS",
  "glucose_context": "random",
  "flag": {
    "status": "normal",
    "label": "Normal",
    "reason": null,
    "emergency": false,
    "message": null
  }
}
```

**No label:** judged as Random (`glucose_context` is `null`), so a normal after-meal value is never shown as High:

<!-- capture: 009-create-glucose-no-label.json response.body keys=value,unit,notes,glucose_context,flag -->
```json
{
  "value": "130",
  "unit": "mg/dL",
  "notes": null,
  "glucose_context": null,
  "flag": {
    "status": "normal",
    "label": "Normal",
    "reason": null,
    "emergency": false,
    "message": null
  }
}
```

**UX instruction:** keep the Random / Fasting / After meal picker mandatory-looking in the entry sheet; an unlabelled fasting reading of 126 to 199 mg/dL will *not* be flagged.

## 5. The patient's record changes the threshold (diabetes, kidney disease)

If the patient's profile conditions, or the active care plan's diagnosis, mention diabetes or kidney disease, blood pressure is High above **130/80** instead of 140/90. The same 135/78 reading:

<!-- capture: 011-create-blood-pressure-diabetic-high.json response.body path=flag -->
```json
{
  "status": "high",
  "label": "High",
  "reason": "Blood pressure above 130/80 mmHg. Tell the family or the patient's doctor for a medical review.",
  "emergency": false,
  "message": null
}
```

<!-- capture: 012-create-blood-pressure-same-value-plain-normal.json response.body path=flag -->
```json
{
  "status": "normal",
  "label": "Normal",
  "reason": null,
  "emergency": false,
  "message": null
}
```

Nothing in the response says *why* the line moved: the condition is never returned. **UX instruction:** do not explain the difference in the app. Show the label and, if you show `reason`, show it as given.

## 6. Editing and listing

Editing a value re-rates it:

<!-- capture: 014-update-heart-rate-re-rated.json response.body path=flag -->
```json
{
  "status": "high",
  "label": "High",
  "reason": "Heart rate above 100 bpm. Tell the family or the patient's doctor for a medical review.",
  "emergency": false,
  "message": null
}
```

The list is newest first, and each row carries its own flag (two rows trimmed):

<!-- capture: 015-list-mixed.json response.body first=2 keys=vital_type,value,unit,notes,glucose_context,flag -->
```json
[
  {
    "vital_type": "heart_rate",
    "value": "130",
    "unit": "bpm",
    "source": "manual",
    "recorded_at": "2026-11-25T17:25:09.332401",
    "notes": null,
    "id": "3c78e66c-6651-44e7-9e62-6c232739d03a",
    "patient_id": "59e25e8c-81cc-4055-afd3-1aae1dfa4364",
    "recorded_by_user_id": "0ccb19bc-b91e-4ea7-aa91-fe21d55eee12",
    "flag": {
      "status": "high",
      "label": "High",
      "reason": "Heart rate above 100 bpm. Tell the family or the patient's doctor for a medical review.",
      "emergency": false,
      "message": null
    },
    "glucose_context": null
  },
  {
    "vital_type": "blood_pressure",
    "value": "135/78",
    "unit": "mmHg",
    "source": "manual",
    "recorded_at": "2026-11-25T17:25:09.328338",
    "notes": null,
    "id": "24c05dff-32cf-4547-9b97-e2f2f81d93a4",
    "patient_id": "59e25e8c-81cc-4055-afd3-1aae1dfa4364",
    "recorded_by_user_id": "0ccb19bc-b91e-4ea7-aa91-fe21d55eee12",
    "flag": {
      "status": "normal",
      "label": "Normal",
      "reason": null,
      "emergency": false,
      "message": null
    },
    "glucose_context": null
  }
]
```

The caregiver's Client Profile (`GET /caregiver/bookings/{booking_id}/client`) carries the same two fields inside `latest_vitals` (items trimmed):

<!-- capture: 016-caregiver-client-profile-latest-vitals.json response.body.latest_vitals first=2 -->
```json
[
  {
    "vital_type": "heart_rate",
    "value": "130",
    "unit": "bpm",
    "recorded_at": "2026-11-25T17:25:09.332401",
    "flag": {
      "status": "high",
      "label": "High",
      "reason": "Heart rate above 100 bpm. Tell the family or the patient's doctor for a medical review.",
      "emergency": false,
      "message": null
    },
    "glucose_context": null
  },
  {
    "vital_type": "blood_pressure",
    "value": "135/78",
    "unit": "mmHg",
    "recorded_at": "2026-11-25T17:25:09.328338",
    "flag": {
      "status": "normal",
      "label": "Normal",
      "reason": null,
      "emergency": false,
      "message": null
    },
    "glucose_context": null
  }
]
```

## 7. UX consequences the FE must surface

1. **Critical:** show `message` ("Call 112 now") next to the badge wherever the reading appears. It is the only emergency instruction in v1; there is no state or city line.
2. **High and Low are not emergencies.** Do not use the Critical red. Both apps use amber for High, blue for Low, green for Normal, red for Critical.
3. **A reading can change colour after an edit** (a patient fixes a typo, or a diabetes diagnosis is added to the profile and the same BP now reads High). Re-fetch rather than caching a flag.
4. **No badge ≠ Normal** (sleep, weight, unreadable rows).
5. **Do not compute a flag in the app, even as a fallback when offline.** A wrong "Normal" can delay care and a wrong "Critical" causes panic. Show the reading unrated until the server answers.
6. The footnote under the grid says the badges follow the clinical thresholds set for Oakly and are not a diagnosis; keep it.

## 8. Not exercised live / open assumptions

| Item | Status |
|---|---|
| Care plan **medicines** feeding the beta-blocker context | Read by the server but changes no status today (heart rate below 60 is Low for everyone); no live example possible |
| Care plan **diagnosis** feeding the 130/80 rule | Covered by an integration test (`test_the_active_care_plan_diagnosis_counts_too`), not captured |
| Legacy rows (a stored `banana`, `300/20`) | `flag: null`; covered by `test_list_flags_every_reading_and_leaves_weight_and_sleep_unflagged`, not captured (the write API refuses new ones) |
| Glucose 121 to 125 mg/dL fasting, random 150 to 199 | Normal in this build; the clinician's text leaves these open (see the SOP's questions) |

## 9. Verification table

| Behaviour | Verified |
|---|---|
| `flag` and `glucose_context` on create, update, list | live (captures 001 to 015) |
| `latest_vitals[].flag` on the caregiver Client Profile | live (capture 016) |
| Critical: `emergency` true and the 112 `message` | live (002, 005) |
| Sleep `flag: null` | live (010) |
| Glucose label from `notes`, unlabelled treated as random | live (007 to 009) |
| °F converted | live (006) |
| Diabetes in the profile lowers the BP line | live (011, 012) |
| Care plan diagnosis, legacy rows, beta blocker context | test only (section 8) |
| Rendering in the apps | Jest only; no simulator pass yet (visual pass pending) |
