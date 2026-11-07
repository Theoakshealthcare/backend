# SOP: caregiver vitals access, grant / revoke / list (DP1-33)

| | |
|---|---|
| **What shipped** | A patient can grant, list and revoke a caregiver's access to their own vitals; a caregiver can list the patients who granted them access. Responses carry `caregiver_name` and `patient_name`. |
| **Branch / commits** | `fix/profile-vitals-validation`, commit `fix(vitals): caregiver access grant, revoke and list compared users.id ...`. Not pushed. |
| **Migration** | None. |
| **FE guide** | [`docs/fe-integration-guide-patient-profile-vitals.md`](../fe-integration-guide-patient-profile-vitals.md) section 5, rendered from captures 301 to 330. |

## Why
`POST /vitals/access/grant` and `/revoke` compared `current_user.id` (`users.id`) with `patient_id` in the body (`patients.id`), and `GET /access/list` listed by `users.id`. They are different ids, so a patient always got `403 Only the patient can grant access.` and an empty list. **Reproduced first:** the new tests failed against the unchanged code (a patient's grant answered 403; one path answered 500). The grant table's foreign keys point at `patients.id` and `caregivers.id`, so the data model was right and the endpoints were wrong. This is health-data access control.

## How
| Decision | Why / alternative rejected |
|---|---|
| The endpoint resolves the signed-in user's **own** `Patient` row and requires `body.patient_id == that id`; anything else is `403 You can only manage access to your own vitals.` A non-patient is `403 Only the patient can grant/revoke access.` | Nobody can act for another patient; the token decides who the patient is. Rejected: dropping `patient_id` from the body (a contract change for no gain; the check is the same). |
| Grant requires an **existing, active** caregiver: unknown, user-id-instead-of-caregiver-id or deactivated is `404 Caregiver not found.` | A foreign-key failure was a 500; a deactivated (erased) caregiver must not be grantable. |
| **Revoke ends every active row** for the pair. | No unique index on active grants, so a race can leave two; revoking only the first would leave the caregiver with access. |
| `GET /access/list` works for both roles: a patient gets their active grants (`caregiver_name`), a caregiver the patients who granted them (`patient_name`), both active only; no profile gives `[]`. | The brief: "a caregiver sees only patients who granted access". |
| `caregiver_name` / `patient_name` are properties on `PatientCaregiverAccess` (no column). | No migration. |
| **`vitals_access.caregiver_can_access_vitals` is untouched**: an `in_progress` visit with the patient, or an active grant. The visit rule is independent: **a revoke does not end an in-progress visit's access.** | Pinned by tests and stated in the guide so the UI does not promise more. |

## What's involved
`app/api/v1/endpoints/vitals.py` (the three access routes), `app/services/patient_caregiver_access.py` (revoke-all, `list_granted_to`, eager loads), `app/schemas/patient_caregiver_access.py` (two optional name fields), `app/db/models/patient_caregiver_access.py` (two properties), `app/tests/test_vitals_grants.py` (new, 19 tests).

## Verification
- Tests: grant then the caregiver reads with no visit; repeat grant is one active row; patient list (excludes revoked and other patients'); revoke ends read, write, edit, delete, download at once and keeps the patient's record; re-grant after revoke; revoke closes two raced rows; revoke twice and never-granted are 404; **another patient, a caregiver, user ids and no token cannot grant or revoke**; unknown and deactivated caregivers 404; profile-less user 403; a grant gives access to that patient only and to that caregiver only; caregiver list shows only granting patients; the in-progress rule still gives access (and survives a revoke), accepted and completed visits still give none without a grant.
- `test_vitals_access` (12), `test_account_deletion` (patient erasure revokes grants), `test_caregiver_client_profile` re-run, unchanged.
- **Live:** captures 300 to 330.

- **Opus review** (one round, read-only): no way to grant, revoke or list for someone else, no leak through the new name fields, no regression of the in-progress rule, no validation breakage. Minors listed below.

## Operate / roll back
Revert the commit: the three routes return to the broken comparison. No data change; grants made meanwhile stay valid because the table and `has_access` are unchanged.

## Follow-ups
- Unique partial index on active grants `(patient_id, caregiver_id) WHERE revoked_at IS NULL` (migration, after `0022`); revoke-all already covers the symptom.
- A revoked/granted event for the audit log and a push to the caregiver ("X shared their vitals"): not built.
- `GET /vitals/{id}/insights` still sends health data to Gemini for anyone with access; consent copy belongs in the apps.
- The patient app screens for sharing are not built; the caregiver app has no "shared with me" list yet.
- **Suspension does not cut vitals access** (review): suspending a caregiver does not deactivate the user, and neither the grant nor `caregiver_can_access_vitals` looks at `profile_status` / `suspended_at`, so a suspended caregiver keeps grants and in-progress visits. Product call whether suspension ends clinical access.
- `recorded_by_user_id` comes from the request body and is never checked against the caller (before this change too): set it from `current_user.id`.
