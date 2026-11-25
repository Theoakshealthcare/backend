# SOP: vitals flags, a clinician-approved Normal / High / Low / Critical on every reading (slice 1b)

| | |
|---|---|
| **What shipped** | A pure flags engine (`app/services/vital_flags.py`) and two additive fields, `flag` and `glucose_context`, on every vitals response: `POST /vitals/`, `PUT /vitals/{id}`, `GET /vitals/{patient_id}`, and `latest_vitals[]` of `GET /caregiver/bookings/{id}/client`. No migration. Backend PR: see the PR for branch `feature/vitals-flags`; apps PR on `feature/vitals-flags-badges` (monorepo). FE guide: [fe-integration-guide-vitals-flags.md](../fe-integration-guide-vitals-flags.md). |
| **Why** | The vitals badges were off until a clinician signed thresholds. The clinical lead supplied them in the clinician review pack (section 3, 7 Nov pack, annotated). Families act on a colour or a word beside a reading, so the rating must be one tested, server-side rule, not a copy in each app (the retired patient app had inconsistent client-side rules, which the pack cites). |
| **Verification** | `test_vital_flags.py` (107 cases, no database, every boundary), `test_vitals_flags_api.py` (11, the wiring), `test_caregiver_client_profile.py` (updated), the vitals validation, access and grant suites, all green locally on a scratch Postgres (221 passed). Live captures: `test_capture_vitals_flags.py` into `docs/fe-captures/vitals-flags/`. |

## Thresholds (the clinical lead's numbers; one table in the module docstring)

| Vital | Normal | Flag | Critical (emergency) |
|---|---|---|---|
| Heart rate | 60 to 100 bpm | high above 100, low below 60 | below 50 |
| Blood pressure | 120/80 to 140/90 | high above 140/90 (above 130/80 for diabetes or kidney disease), low below 100/60 | top number below 90 |
| Temperature | 36.5 to 37.5 °C | high above, low below | none |
| Respiratory rate | 12 to 20 | high above 20, low below 12 | none |
| Blood glucose | below the high line, at least 70 mg/dL | fasting 126 mg/dL (7.0 mmol/L) or more, random 200 (11.1) or more | none |
| Weight, sleep | never flagged | | |

A critical reading sets `emergency: true` and `message: "This may be an emergency. Call 112 now."` (the pack's own wording and its one emergency number).

## How, and the decisions

| Decision | Why / alternative rejected |
|---|---|
| **Compute on read; no migration, nothing stored.** | The flag is a pure function of value, unit, label and the patient's record. A stored flag would go stale when a threshold or a diagnosis changes. Rejected: a `flag` column (a migration and a backfill for no gain; migration heads are contested by slices 1a and 1d). |
| The glucose label stays in `notes` (`RBS`/`FBS`/`PP`); the API also returns it as `glucose_context`. | Both apps already write it there, so old data and old builds work unchanged. Rejected: a new column (migration, and the apps would need to send a new field). |
| Patient context is read from free text by keyword (`Patient.medical_conditions`, `Patient.medications`, the active care plan's diagnosis and active medicine names). | The record is typed text, not coded. A missed match only means the standard thresholds apply. |
| **The response never names the condition.** `reason` states the number crossed. | A caregiver with a vitals grant may not be allowed to read the diagnosis (found by the automated security review of the first commit). The only trace is the 130/80 line itself. |
| Weight, sleep, unknown types and non-numeric values get `flag: null`. | "No badge" is honest; "Normal" for a reading nobody rated would not be. |
| Out of range = `is_out_of_range(flag)`; `stored_reading_is_out_of_range(db, reading)` is the **hook for slice 1c (alerts)**. | Alerts must not re-implement the rules. Alerts, notifications and routing are not built here. |
| An unreadable record makes `patient_context` fall back to the standard thresholds and log, never raise. | A vitals list must not fail over a context lookup. |

## Assumptions where the pack was silent (questions for the clinical lead)

1. **Glucose below 70 mg/dL (3.9 mmol/L) is Low.** The pack gives "Normal 70 to 120" and no Low line.
2. **Glucose from 121 to 125 mg/dL fasting, and random 121 to 199, are Normal.** The pack's normal band stops at 120 but flags only at 126 (fasting) and 200 (random); we flag only at the stated lines.
3. **After meal and unlabelled are judged by the random line (200).** The pack names the three labels but gives no after-meal figure.
4. **Blood pressure above the high line and below the low line at once (160/55) is High.**
5. **The beta-blocker note changes nothing today**: heart rate below 60 is already Low for everyone. The context (hypertension plus a beta blocker) is read and kept for slice 1c to word a message for the prescriber. Please confirm that was the intent, or whether such patients should flag at a higher line.
6. **Diastolic-only comparisons** ("above 140/90" is read as top above 140 or bottom above 90; "below 100/60" as top below 100 or bottom below 60).
7. °F entries are converted to °C and rounded to one decimal before comparing (the pack struck °F out of its table, but the apps still accept it).

## What's involved

- `app/services/vital_flags.py`: thresholds, `evaluate_reading`, `context_from_text`, `patient_context`, `reading_flag`, `vital_response`, `is_out_of_range`, `stored_reading_is_out_of_range`.
- `app/schemas/patient_vital.py` (`VitalFlagOut`, `flag` and `glucose_context` on `PatientVitalResponse`), `app/schemas/caregiver_client.py` (`ClientVital`), `app/api/v1/endpoints/vitals.py`, `app/services/caregiver_clients.py` (`_latest_vitals`).
- Tests: `app/tests/test_vital_flags.py`, `test_vitals_flags_api.py`, `test_capture_vitals_flags.py` (capture only, `CAPTURE_VITALS_FLAGS=1`).
- Apps (monorepo `feature/vitals-flags-badges`): regenerated `packages/api-client`; caregiver Vitals tab, vital page, history and Client Profile; patient Vitals home, monitoring page and history. The admin console shows no vitals, so nothing changed there.

## Operate / roll back

Nothing to deploy beyond the code and nothing to migrate. To change a number, edit the constants at the top of `vital_flags.py` and the matching cases in `test_vital_flags.py`, on the clinical lead's say-so. Roll back by reverting the PR: the fields disappear and the apps (which treat a missing `flag` as no badge) keep working.

## Follow-ups

- Slice 1c: alerts to the family contact and the admin queue, using `stored_reading_is_out_of_range`.
- The questions above go to the clinical lead; each is one constant.
- CSV export has no flag column (additive change only, by design).
- Family view is out of v1 apps; the data already carries the flag, so a later family screen needs no backend work.
