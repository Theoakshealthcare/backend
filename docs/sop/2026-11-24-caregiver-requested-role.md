# SOP: caregiver "I apply as" (requested roles) on setup step 2

| | |
|---|---|
| **What shipped** | The caregiver says which role(s) they apply as in setup step 2 (`applying_as`: any of `nurse`, `doctor`, `physiotherapist`, `caregiver`, `nutritionist`, 1 to 5, no duplicates). It is stored as a **request**. The admin caregiver detail returns it as `requested_skills`. Migration `0028_caregiver_applying_as`. PR: Theoakshealthcare/backend (branch `feat/caregiver-requested-role`, base `develop`). The monorepo half (app step 2, console line and pre-ticked "Add skills" dialog) is a second PR. |
| **Why** | An admin cannot approve a caregiver without at least one skill (`caregiver_skills`), but the caregiver never says which role they apply as: step 2 only asks specialization, years, preferred hours and service areas. Admins had to guess. Skills stay admin-confirmed on purpose, so nobody can claim to be a doctor; this only tells the admin what to confirm. Owner decision 2026-11-24 (not drawn in Figma). |
| **Not changed (the rule)** | Approval rules are exactly as before: it needs at least one admin-assigned skill. `PUT /admin/caregivers/{id}/skills` is still the only thing that grants a skill. Nothing reads `applying_as` for matching, care plans or approval. |

## How

| Decision | Choice | Rejected |
|---|---|---|
| Storage | Nullable `caregivers.applying_as` `text[]` (same type as `service_areas`) with a CHECK that every value is one of the five | A requests table: overkill for a list of at most five strings |
| Where the five values live | `SERVICE_TYPES` and the new `SERVICE_TYPE_LABELS` in `app/db/models/caregiver.py` (labels: Nurse, Doctor, Physiotherapist, Caregiver, Nutritionist) | A second copy of the list |
| Required or optional | **Required on a caregiver's first submission of step 2** (`422` `missing` on `body.applying_as`, nothing saved). **Optional on a re-save** of a row that already passed step 2 or already holds a value: an omitted key leaves the stored value as it is. Rows from before the field stay `null` | Always required (would break a legacy caregiver re-saving step 2); always optional (the owner wants it chosen) |
| "First submission" test | `applying_as` empty **and** `profile_completion_step < 2` (the service knows the row; the schema cannot) | A new flag column |
| Validation | Pydantic, strict: 1 to 5 items from the five-value `Literal`, duplicates rejected, **explicit `null` rejected** (omit the key instead); `422` in the standard FastAPI shape | Silently de-duplicating |
| The missing-field `422` | Raised as `RequestValidationError` from the endpoint so it is byte-for-byte the shape FastAPI gives any other missing field | A custom error body |
| Admin detail | New top-level `requested_skills: [str]` (always present, `[]` when none). `profile.applying_as` also appears (the profile schema gained the field) | Reusing `skills` (would blur request and grant) |
| `unmet_for_approval` | `NO_SKILLS` keeps its code and rule; its message gets "(the caregiver requested: Nurse, Caregiver)" when roles were requested | Auto-granting, or treating a request as a skill |

## What's involved

| Area | Where |
|---|---|
| Model, labels, CHECK | `app/db/models/caregiver.py` (`Caregiver.applying_as`, `SERVICE_TYPE_LABELS`, `ck_caregivers_applying_as`) |
| Migration | `app/alembic/versions/0028_caregiver_applying_as.py` (revises `0027_booking_minutes`; downgrade drops the check and the column) |
| Request/response schemas | `app/schemas/caregiver.py` (`CaregiverProfileStep2.applying_as`, `CaregiverProfile.applying_as`, `RoleOption`, `FormOptionsOut.roles`) |
| Step 2 rule | `app/services/caregiver.py` (`update_profile_step2`, `ApplyingAsRequired`, `get_form_options` now also returns `roles`), `app/api/v1/endpoints/caregiver.py` (the `422`) |
| Admin | `app/schemas/admin_caregiver.py` (`ApplicationDetail.requested_skills`), `app/services/caregiver_approval.py` (`get_detail`, the `NO_SKILLS` message; edits kept minimal because another branch touches this file) |
| Docs and captures | `docs/fe-integration-guide-caregiver.md` (section 4, "I apply as"), `docs/fe-integration-guide-admin.md` (12.15), captures `docs/fe-captures/caregiver/950-958` and `docs/fe-captures/admin/950-954` + `969`, scripts `scripts/capture_caregiver_flow.py` and `scripts/capture_admin_flow.py --section caregiver-roles` |

## Verification

| Check | Result |
|---|---|
| `app/tests/test_caregiver_applying_as.py` (new): stores and returns the roles; no skill created; first submission without it is `422` and saves nothing; unknown, duplicate, empty, `null`, not-a-list and six values are `422`; all five accepted; legacy row re-saves and stays `null`; re-save keeps or replaces; DB CHECK rejects a sixth value; form options; admin detail with and without a request; a caregiver with requested roles and no skill is **not** approvable (`409 NO_SKILLS`, nothing granted), and only the admin's skills call lets approval through | pass |
| `app/tests/test_caregiver_applying_as_migration.py` (new): chain, upgrade adds the nullable column and CHECK on a scratch DB, legacy row stays `NULL`, downgrade removes both and keeps rows, upgrades again | pass |
| Pinning files: `test_caregiver_approval.py`, `test_response_shapes.py` (golden regenerated: `applying_as`, `requested_skills`, `roles` only), `test_kyc_caregiver_api.py`, `test_migrations.py` | pass |
| `ruff check --select E9,F63,F7,F82`, `scripts.ci_smoke`, both `render_fe_guide --check` | clean |
| Live | A full caregiver capture and the admin block were run against a scratch compose stack (db, redis, api, worker; migration `0028` applied at start), then the stack and its volumes were removed |

## Operate / roll back
Deploy as usual: the API container runs `alembic upgrade head`. Roll back with `alembic downgrade 0027_booking_minutes` (the requested roles are lost; nothing else reads them). An app build that does not send `applying_as` gets `422` on a caregiver's first step-2 save, so ship the app change with or before this reaches production (staging first).

Re-capturing: `scripts.capture_caregiver_flow` deletes the **whole** capture folder, including the captures other scripts own (`190`-`244`, `700`+, `800`+). After a run, `git checkout -- $(git ls-files -d docs/fe-captures/caregiver)` restores them, and it needs a fresh database (a second run on the same one stops at the payout batch). The roles captures live in their own numbered block (`950`-`969`) for that reason. Re-rendering also refreshed unrelated examples that had drifted (timestamps, ids, `can_author_care_plans`, a few validation-error bodies).

## Follow-ups
- Monorepo PR: caregiver step 2 multi-select, console "Requested by the caregiver" line and pre-ticked dialog.
- Nothing yet nudges an admin when a caregiver requested a role that no verified licence supports; that is a judgement the admin makes.
