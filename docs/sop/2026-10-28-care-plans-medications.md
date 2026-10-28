# SOP: Care plans, slice B3 (medications)

| | |
|---|---|
| **Date** | 2026-10-28 |
| **Branch / PR** | `feat/care-plans-medications`, cut from `oakly/main` `11f7e48` (B1 #26 and B2 #29 merged). Not pushed, no PR (the lead pushes). |
| **Spec** | [`docs/plans/2026-10-27-caregiver-care-plans-design.md`](../plans/2026-10-27-caregiver-care-plans-design.md) section 14 (B3), 3.3, 3.4, 5.6, 6.1 routes 13-15, decision D4, assumption A7. |
| **Contract** | [`docs/fe-integration-guide-caregiver.md`](../fe-integration-guide-caregiver.md) sections 16.21 to 16.25 (captures `240` to `244`). |
| **Migration** | `0018_care_plan_medications` (design calls it `0017`; B2 took it). |

## What shipped
A nurse records the medicines a client's prescriber ordered on the client's care plan; every caregiver on a live visit for that client reads them. **Record only** (D4): nothing checks a dose or an interaction, nothing reminds anyone, nothing records that a dose was given (administration stays a visit task, ticked like any task, A7).

| Route | Who |
|---|---|
| `POST /caregiver/care-plans/{plan_id}/medications` | nurse |
| `PATCH /caregiver/care-plan-medications/{id}` | nurse |
| `POST /caregiver/care-plan-medications/{id}/stop` `{expected_version}` | nurse |
| `medications` on the full plan (`GET /caregiver/care-plans/{plan_id}`, create/edit responses, the `plan` in a `409`) | any caregiver with access |

### The "not clinical advice" wording (for clinical sign-off)
One constant, `MEDICATION_NOTICE` in `app/schemas/care_plan.py`:

> This is a record of what the prescriber ordered. Oakly does not check doses or interactions and does not remind anyone to give medicines. Follow the prescriber's instructions.

It is the design's 5.6 banner copy, unchanged. The OpenAPI descriptions of the three routes and of the plan's `medications` field are built from the constant (a test reads the generated spec), and the FE guide quotes it for the app's banner. If the clinician changes the words: edit the constant, re-run `scripts.ci_smoke`, re-render nothing (the guide quotes it in prose in section 16.21: update that paragraph by hand).

## Why
Design section 14: medicines are part of what a visiting caregiver needs to know about a client, but the product must not look like it manages them. Hence a record the prescriber's order is copied into, with a banner, and no behaviour attached.

## How (decisions and what was rejected)
| Question | Decision | Rejected |
|---|---|---|
| Storage | `care_plan_medications`: name, strength, dose amount/unit, route, reason, prescriber and note are **one Fernet JSON** (`detail_encrypted`, nullable only for erasure, required by the service); `frequency` (closed set), `times_of_day` (text[] `HH:MM`, at most 12), `start_date`, `end_date`, `status`, `stopped_at` stay plaintext. Same no-plaintext-fallback rule as B1: a bad token is a 500, never a blank. | A column per field (design 3.4: structured values are one encrypted JSON). |
| Dose fields | `dose_amount`, `strength`, `dose_unit`, `route` are short **free text** (60 chars). Nothing parses or compares them. | Numeric dose with units (invites arithmetic, which is clinical logic D4 rules out). |
| Never deleted | A medication is stopped: `status='stopped'` and `stopped_at` set, kept on the plan. A CHECK ties the two (`(status='stopped') = (stopped_at IS NOT NULL)`). Stopping twice is a no-op. No DELETE route. | Hard delete. |
| Stopped is history | `PATCH` on a stopped medication is 409 `medication_stopped`. To restart, record a new one. | Allowing edits (would rewrite history) or a restart route (not in the spec). |
| Version | Every write takes the plan row `FOR UPDATE`, bumps `plan.version` and returns `plan_version`, exactly like goals and tasks. `expected_version`: optional on add, required on PATCH and stop. A no-op PATCH or repeated stop leaves the version alone. | Not bumping (the plan would change under a reader without the version moving). |
| Stop request shape | `POST .../stop` with a JSON body `{expected_version}` (the spec says only "stop"). | A DELETE with a query param (it is not a delete). |
| Cap | At most **40 active** medications per plan (spec 6.1: 40 per plan); stopped ones do not count, so history cannot lock a long-lived plan. | Counting history. |
| Dates | `start_date` defaults to today (Africa/Lagos); `end_date >= start_date` is checked in the service on add and on PATCH against the stored values, and by a table CHECK. | |
| List order | Active first, then stopped, each in the order recorded. | Sorting by name. |
| Access | The B1 resolver unchanged: access through the medication's plan first (one identical 404), then the nurse test (403 `not_a_nurse`), so the 403 never reveals that a medication exists. | |
| Events | `med.add`, `med.update` (field names), `med.stop`: ids and field names only, never medicine text. | |
| Family view | Medications have no `family_visible` column and are in no family access result: not family-visible until a later decision (design 4.4). | |
| Erasure | `wipe_plans_for_subject` nulls `detail_encrypted` for every medication of an erased subject; rows, enums, times, dates and the event trail stay. A booker's erasure leaves the plan of the person they booked for (unchanged rule). | Deleting rows. |
| Rotation | `care_plan_medications.detail_encrypted` added to `reencrypt_health_data.COLUMNS` (`plaintext_fallback=False`); the B2 introspection test finds the new table by name and fails if it were missing. | |

## What's involved
- Migration `app/alembic/versions/0018_care_plan_medications.py` (one table, five CHECKs, one index; downgrade drops it).
- Model `CarePlanMedication` in `app/db/models/care_plan.py`; schemas, `MEDICATION_NOTICE` and the `medications` list on `CarePlanOut` in `app/schemas/care_plan.py`.
- Service `app/services/care_plan_medications.py` (add, edit, stop, `MedicationStopped`); `app/services/care_plans.py` (`seal_json`/`unseal_json`, `_medication_out`, `build_plan`, erasure wipe).
- Routes at the end of `app/api/v1/endpoints/caregiver_care_plans.py`; `scripts/reencrypt_health_data.py`.
- Tests: `test_care_plan_medications.py`, `test_care_plans_b3_access.py`, `test_care_plans_b3_security.py`, `test_care_plan_medications_migration.py`, capture script `test_capture_care_plans_b3.py`. Changed: `test_care_plans_core.py` (plan key set), `test_care_plans_security.py` (table list), `test_care_plan_ticks_migration.py` (0017 is no longer the head).
- B1 and B2 captures were re-run (every plan body now has `medications`), so many `docs/fe-captures/caregiver/2xx` files changed only in ids, timestamps and that key.

## Verification
**Full local CI parity (2026-10-28):** `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke` (CI's dummy env vars; 180 operations), both `render_fe_guide --check`, and the full pytest under the shared lock: **3255 passed, 5 skipped** (33 min). 58 new tests across four files plus the capture script (skipped unless `CAPTURE_CARE_PLANS=1`).

Highlights: add (full and minimal), edit merge of the encrypted document (only sent fields change, null clears, required fields cannot be nulled, dates re-checked against stored ones), stop and stop-again, stopped medication not editable, 40-active cap and a stop freeing a place, stale-version 409 carrying the current plan, plan completed refuses writes but keeps showing medications, nurse-only writes with every assigned caregiver reading; the identical 404 for non-live, dropped-out, erased, stranger, another client's medication id and unknown ids; ciphertext at rest, rotation, bad token is a 500 never a blank, events hold no medicine text, erasure nulls the text and keeps rows and trail, a booker's erasure leaves the booked-for person's medications; table constraints and the migration round trip; the notice in the OpenAPI spec. Captures `240` to `244` are real responses from the app on a real Postgres (test harness, not the compose stack).

**Opus review (one pass, read-only):** APPROVE, no HIGH or MED. Applied: `HH:MM` pattern uses `[0-9]` (a Unicode-digit time would have passed `\d`) and a PATCH no-echo test. The rest went to Follow-ups. No re-review.

## Operate / roll back
Deploy: `alembic upgrade head` (`0018` after `0017`). No config. Roll back by reverting and `alembic downgrade 0017_care_plan_ticks_and_notes`, which drops every medication; do not downgrade once real plans carry medications. Key rotation is the existing `scripts/reencrypt_health_data.py`.

## Deviations from the design doc
- Migration number `0018` (see above).
- `POST .../stop` takes `{expected_version}`; extra response fields `prescribed_by` and `stopped_at`; extra code 409 `medication_stopped`; `dose_amount`/`strength` are text.
- The 40-medication limit counts active medications only.
- `plan.version` bumps on medication writes (consistent with goals and tasks).

## Follow-ups
- **Go-live gate:** a clinician signs off `MEDICATION_NOTICE` (and the mobile banner built from it, M3).
- A stopped medication cannot be corrected (a typo in history needs a new record). If nurses ask, add an audited correction path rather than relaxing the edit rule.
- The reason a medication was stopped is not recorded (it would be health text; it would go inside `detail_encrypted`). Add if the clinician wants it.
- No per-dose record and no reminders (A7, non-goals): needs clinical sign-off first.
- Admin API (B4) must reuse `care_plan_medications.add_medication/update_medication/stop_medication` with an admin actor; the services currently take a `Caregiver` for the event actor.
- (Opus review, LOW) The 40 limit counts active medications only, so stopped history is unbounded and every plan GET (and every 409 reload) decrypts all of it. Fine for now; add a cap or paging of stopped ones if a plan ever accumulates hundreds.
- (Opus review, LOW) B1's and B2's `HH:MM` patterns (`TaskIn.time_of_day`, `TaskPatch.time_of_day`) still use `\d`, which pydantic's regex engine treats as any Unicode digit; B3's `HHMM` uses `[0-9]`. Align B1/B2 in a small fix.
