# SOP: vitals are validated when saved (DP1-16)

| | |
|---|---|
| **What shipped** | `POST /vitals/` and `PUT /vitals/{id}` accept only the six vital types the apps use, in their units, within plausible ranges, with a sentence as the `422` message. |
| **Branch / commits** | `fix/profile-vitals-validation`, commit `fix(vitals): only the six known vital types ...`. Not pushed. |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-patient-profile-vitals.md`](../fe-integration-guide-patient-profile-vitals.md) section 4, rendered from captures 200 to 222. |

## Why
The live captures saved `300/20` as a blood pressure and a `banana` of `"not a number"` (DP1-16: no client or server validation). The server stored anything.

## How
| Decision | Why / alternative rejected |
|---|---|
| New pure module `app/services/vital_rules.py`: `VITAL_SPECS` (type, units, bounds) and `check_reading`. Used by `PatientVitalCreate` (a `model_validator`) and by the update route. | One place for the rules; the response model is **not** validated, so rows saved before still list. |
| Vocabulary = the two apps': `heart_rate`, `blood_pressure`, `temperature` (`°C`/`°F`), `respiratory_rate`, `blood_glucose` (`mg/dL`/`mmol/L`), `sleep_duration`. Glucose context stays in `notes` (`RBS`/`FBS`/`PP`), sleep times in `notes`. | Read from `apps/caregiver/src/vitals/vitalsModel.ts` and `apps/patient/src/domain/vitals.ts`. No new sub-types. |
| Bounds are the patient app's entry ranges; sleep keeps the caregiver app's rule (more than 0, at most 24). Systolic must be above diastolic. Temperature and glucose ranges are converted per unit. | Never stricter than what an app's own form lets through, so a reading entered in either app passes. These are typing-mistake guards, not clinical limits. |
| Decimals allowed for every type (blood pressure too); `36,8` refused; a missing `unit` is allowed and checked against the type's default. | The patient app's keypad is a decimal pad and a unit was always optional on the wire. Tightening to whole numbers is a follow-up. |
| The `422` uses `PydanticCustomError` (`type: vital_invalid`) so `detail[0].msg` is a sentence both apps already display. | |
| `PUT` judges the **whole** reading after the edit (new value against the stored unit); an edit of `notes` alone is never re-judged. | A value change must stay plausible for its unit; an old bad row can still get a note. |
| Value is stored trimmed; `120 / 80` is stored as `120/80`. | |
| Rejected: a database CHECK or migration (rows exist and the value column is encrypted text). | |

## What's involved
`app/services/vital_rules.py` (new), `app/schemas/patient_vital.py` (`PatientVitalCreate` validator), `app/api/v1/endpoints/vitals.py` (`update_vital`), `app/tests/test_vitals_validation.py` (new, 53 tests).

## Verification
- 13 accepted payloads (every type and unit the apps send, glucose and sleep notes, no unit) and 25 refused ones (the captured `300/20` and `banana`, inverted and malformed pressure, ranges at both ends, wrong unit, comma, exponent, sign), the caregiver app recording during an in-progress visit, the 403 for someone else's record, **legacy rows (`banana`, `300/20`) still list, download and delete**, edits judged whole, a notes-only edit of a legacy row allowed.
- `test_vitals_access` (12) re-run: unchanged. Neighbours: `test_caregiver_client_profile`, `test_capture_client_profile`, `test_secrets_encryption`, `test_gemini_service`.
- **Live:** captures 200 to 222.
- **Existing data** is not touched (no migration, no backfill, reads unvalidated).

## Operate / roll back
Revert the commit. Nothing to configure. **Deploy order:** safe either way, but the caregiver app (which only checked "a positive number") will start seeing `422` for implausible readings, so its entry form should show `detail[0].msg`.

## Follow-ups
- Tighten blood pressure, heart rate and respiratory rate to whole numbers once the patient app enforces it.
- `recorded_at` is not checked (a future timestamp is accepted).
- The AI insights call still sends whatever old rows exist, including junk types, to the model.
- Clinical review of the ranges if they should ever drive "Critical/Normal" (nothing does today).
