# SOP: fix: caregivers got a 500 on every vitals call

| | |
|---|---|
| **Date** | 2026-10-27 |
| **Branch / PR** | `fix/vitals-caregiver-access` off `oakly/main` f4b2f80 (also carried by `feat/caregiver-client-profile`, which builds on it). |
| **Found by** | Writing the first tests for `/vitals` while building the caregiver Vitals tab (monorepo `feat/caregiver-vitals`). The endpoint had no tests. |

## What shipped
A caregiver (with an in-progress visit or a patient grant) can now read, write and download a patient's vitals. Before, **every** caregiver call to `GET/POST/PUT/DELETE /vitals/...` (list, create, update, delete, download, insights) answered `500`.

## Why
`can_access_vitals` filtered bookings with `Booking.status.in_(["active", "in_progress"])`. `active` is not a `BookingStatus` (the values are `pending`, `accepted`, `en_route`, `in_progress`, `completed`, `cancelled`, `declined`), so Postgres rejected the query: `invalid input value for enum bookingstatus: "active"`. The 403 for a caregiver with no visit never happened either; the query ran before the explicit-grant check could answer. Patients were unaffected.

## How
The rule moved into `app/services/vitals_access.py::caregiver_can_access_vitals` and uses `BookingStatus.IN_PROGRESS` only. Same rule as intended: **in-progress visit with that patient, or an unrevoked grant**. `can_access_vitals` in the endpoint now calls it. No schema, migration or response change. Rejected: keeping a list of statuses (the nonexistent `active` is how this broke; accepted and on-the-way visits are deliberately not enough).

## What's involved
`app/services/vitals_access.py` (new), `app/api/v1/endpoints/vitals.py` (`can_access_vitals`), `app/tests/test_vitals_access.py` (new, 12 tests).

## Verification
`pytest app/tests/test_vitals_access.py`: 12 pass against a real Postgres (testcontainers). The first test failed with the 500 before the fix. Covered: in-progress caregiver can create and list; accepted, en route, pending, completed, cancelled, declined are refused with `403 {"detail": "Access denied"}`; another caregiver's visit gives nothing; a grant gives access until revoked; a caregiver account with no profile is refused; a patient reads only their own; the `start` filter (UTC without an offset, as the caregiver app sends it) and the CSV download work. The full suite is run with the branch that carries this (see its SOP).

## Operate / roll back
Revert the commit; no data change. Until it is deployed, the caregiver app's Vitals tab shows its error state (the 500) instead of vitals.

## Follow-ups
- `POST /vitals/` trusts `recorded_by_user_id` from the body: a caregiver can record a reading "by" someone else. Set it from the caller.
- `POST /vitals/access/grant` and `GET /vitals/access/list` compare the patient's **user id** with the patient id (`current_user.id` vs `patient_id`), so a patient can never grant or list access. The explicit-grant path therefore cannot be reached through the API yet.
- `vital_type` is free text, there is no glucose context field, and the list has no paging (the app sends `start`).
- The caregiver app cannot see which patients granted it access; it offers vitals only for in-progress visits.
