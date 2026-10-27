# SOP: Caregiver client profile (backend endpoint)

| | |
|---|---|
| **Date** | 2026-10-27 |
| **Branch / PR** | `feat/caregiver-client-profile` off `oakly/main` f4b2f80, **stacked on** `fix/vitals-caregiver-access` (0a3ea84, its own SOP: `2026-10-27-vitals-caregiver-access-fix.md`). Not pushed. |
| **App side** | Monorepo `feat/caregiver-client-profile` (stacked on `feat/caregiver-vitals`): the "Client profile" screen. |
| **Contract** | [`docs/fe-integration-guide-caregiver.md`](../fe-integration-guide-caregiver.md), "Client profile" in section 7 (captures `190`-`195`). |

## What shipped
`GET /api/v1/caregiver/bookings/{booking_id}/client`: who the visit is for. Name, age, sex, emergency contact, medical history **only if stored**, and the latest vitals when the vitals rule allows them. Read-only, no migration. Care plans are not built, so there is no care plan field.

## Why
Gap analysis section 5: the caregiver app showed the client only as booking details. The design's Client Profile (age, sex, latest vitals, history, emergency contact) needs a caregiver-scoped endpoint, allowed only while assigned. Privacy rule kept: no clinical field before assignment.

## How (brainstorm and decisions)
| Question | Decision | Rejected |
|---|---|---|
| Key | The **booking id**, not the patient id. Authorization is "this booking is mine and live", and the booking says who the client is. | `/clients/{patient_id}`: a patient row can be a booker with several relatives, so a patient id does not say whose data a caregiver is looking at. |
| Who | Assigned caregiver, booking `accepted`, `en_route` or `in_progress`. A **suspended** caregiver only for `in_progress` (same as starting a visit: finish what is under way, no new ones). | Any past booking (completed visits would keep the data readable). |
| Refusals | **One 404 `{"detail": "Client not found"}`** for unassigned, only-offered, other caregiver, wrong status, missing booking, erased or inactive patient, caregiver without a profile. No existence oracle. A patient token is `403` (the usual caregiver-only dependency), no token `401`. | 403 for "not yours" and 404 for "missing" (tells a caller which booking ids exist). |
| Someone else's visit | `booking_for` other than `myself` (case-insensitive: the patient app sends `Myself`, WhatsApp `myself`): the patient row is the **booker**. Name, age and sex come from the care recipient if there is one, else `"Client"`; medical history, photo and vitals are `null`; the emergency contact is the booker, labelled `Booked this visit`. | Showing the booker's allergies and photo as the client's (found by the Opus review). |
| Vitals | `latest_vitals`: newest of each type via `caregiver_can_access_vitals` (shared with `/vitals`, see the fix SOP), `null` when not allowed, `[]` when allowed but empty. No status field. Only for a visit booked for oneself, because readings are filed under the booker's patient id and cannot be told apart per relative. | Per-recipient vitals (needs `care_recipient_id` on `patient_vitals`, a migration: follow-up). |
| Left out | Address, coordinates, email, patient phone, booking `condition` (plain and encrypted), `special_instructions`, doctor and file fields of medical records, care plan. The schema is a closed list. | |
| Headers | `Cache-Control: no-store`. Nothing is logged. | |

## What's involved
`app/api/v1/endpoints/caregiver_clients.py` (route), `app/services/caregiver_clients.py` (access and assembly), `app/schemas/caregiver_client.py`, `app/api/v1/api.py` (mount under `/caregiver`), `app/services/vitals_access.py`. Tests: `app/tests/test_caregiver_client_profile.py` (27), `app/tests/test_vitals_access.py` (12). Captures: `app/tests/test_capture_client_profile.py` (skipped unless `CAPTURE_CLIENT_PROFILE=1`), `docs/fe-captures/caregiver/190`-`195`.

## Verification
- **Tests (27)**: access for each live status and refusal for pending, completed, cancelled, declined; another caregiver and a missing booking get the identical 404; an offered-only caregiver sees nothing and no patient text; drop-out loses access; suspended caregiver (accepted and en route refused, in progress allowed); erased patient; patient token 403 / anonymous 401 / bad uuid 422; no caregiver profile; exact response keys and no leak of address, coordinates, email, phone, condition (plain and WhatsApp-encrypted), special instructions, doctor and file fields; age rounding; `no-store`; vitals newest-of-each, `[]`, `null`, grant; booked-for-someone-else (WhatsApp, and an app booking with no recipient), `Myself`; a vital with a null `recorded_at`.
- **Opus review** (one read-only pass, one fix round): no access-control or oracle issue. Four real findings fixed (the `Myself` casing; vitals and the booker's name shown as the relative's; the family contact's relationship; a null `recorded_at` that could 500). Its other finding is the grant/revoke bug listed below.
- **Captures** `190`-`195` are real responses from the app on a real Postgres via the test harness, not the compose stack (not run: RAM rule).
- Full pytest and every CI step: see the report.

## Operate / roll back
Revert the commits; no data or config. Deploy the vitals fix with it (the profile reuses its rule).

## Follow-ups
- `patient_vitals` has no care recipient: readings for several relatives of one booker are mixed under the booker. Add `care_recipient_id` and show vitals for visits booked for others.
- Grant and revoke (`/vitals/access/*`) compare a user id with a patient id, so a patient can never grant access; `recorded_by_user_id` is trusted from the body (see the vitals fix SOP).
- The profile has no audit entry for who read which client; add if a clinical-governance review wants access logs.
- An app booking for a relative has no recipient on file, so the client is just `"Client"`. Capture the relative's name and age in the app booking.
- Capture calls belong in `scripts/capture_caregiver_flow.py` (compose stack) the next time it is run; it rewrites the capture directory.
