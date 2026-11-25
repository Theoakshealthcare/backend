# SOP: clinician-approved care templates, caregiver own-documentation space, nurse 2-hour note window

| | |
|---|---|
| **What shipped** | (1) Four care-plan templates carry the clinical lead's approved text (version 2). (2) The Elderly template can be started and edited by any caregiver, not only a nurse. (3) A private documentation space where a caregiver records their own notes for a visit, with an audited read-only list in the admin console. (4) The assigned nurse can add care-plan notes for 2 hours after the visit is completed. |
| **Branch / PR** | backend `feature/care-templates-docs-space` (PR to `develop`; cut from `feature/bank-name-review` so migration `0031` follows `0030`); monorepo `feature/care-templates-docs-space` (PR to `main`; cut from `feature/bank-name-review`). Not merged: waits for the owner's "merge #N" and green CI. |
| **Migration** | `0031_care_templates_docs_space` (down_revision `0030_bank_name_review`): `care_plan_templates.caregiver_can_use`, the four template rewrites, table `visit_documentation`. Single head. |
| **FE guide** | [`docs/fe-integration-guide-care-templates-docs-space.md`](../fe-integration-guide-care-templates-docs-space.md), rendered from `docs/fe-captures/care-templates-docs-space/` (checked in CI). The caregiver and admin guides got a "changed 2026-11-25" note. |

## Why
The clinical lead's answers to the Clinician Review Pack (7 Nov) approved the five care templates "as annotated": add "contact the family and inform the family's doctor" (diabetes, cardiac, and what "report at once" means), a pain-record task for post-surgery, the discharge-instruction wording for cardiac activity, an Elderly template handled "either Nurse or Caregiver or both", and "a space for the caregiver to make own documentation". The owner decided that space is not shown to the family, and answered yes to a 2-hour grace for the assigned nurse to add care-plan notes after the visit window.

## How
| Decision | Why / alternative rejected |
|---|---|
| **Template text is changed by a migration that edits the stored body and bumps `version` 1 to 2**, not by a new seeding script. | Templates were seeded by `0016`; plans copy the template at creation and record `template_version`, so existing plans keep their text. The migration applies named edits (rename a task, add a task, add an instruction) and the downgrade reverses exactly those, so it is safe even if a body was touched. Rejected: re-seeding rows (would break `template_version` meaning). |
| **`clinical_review_status` is left `pending`.** | Sign-off is the admin action `POST /admin/care-plan-templates/{id}/review` (`careplans.templates`), and it records who signed. A migration cannot sign in a clinician's name. The apps keep showing "Not yet clinically reviewed". |
| **Family-and-doctor line added as a special instruction on all four templates (including Stroke).** | The clinician's section 5 answer to "what does 'report at once' mean" points to it; the brief listed all four. |
| **The pain "record" is a task, not a vital.** | Pain is not one of the six vitals (clinician: weight and sleep not needed; pain was "next update"). The caregiver records detail in a note or their own documentation. |
| **Elderly template for any caregiver: a boolean `care_plan_templates.caregiver_can_use`.** | One flag on the existing row, no new role system. Rule (`care_plan_access.may_use_template` / `may_author_plan`): a nurse may use any template; anyone else only a flagged one, and may edit (header, goals, tasks) only a plan whose template is flagged. **Medicines stay nurse-only** (`can_edit_medications`), because the caregiver-medication change (with a supervisor step) is a separate slice. Rejected: a per-plan "owner" flag. |
| **Template list is `200` for everyone, filtered; candidates open to any caregiver.** | A non-nurse needs to find the Elderly template; `403` on the list gave them nothing to pick. A nurse-only template preview is still `403 not_a_nurse`. |
| **Nurse grace reuses the existing note window.** `VOICE_NOTE_POST_VISIT_GRACE_MINUTES` (120) counted from `visit_windows.ended_at`; no new setting. | The task said to reuse and extend the windows. One number now moves notes, voice notes, care-plan notes and documentation together. |
| **Grace is nurse-only and covers notes only.** A separate access path (`resolve_grace_access_for_plan` / `_for_booking`) for an approved nurse's completed booking inside the window; used by the plan read, the observations list and add, and the visit summary. Edits, goals, tasks, medicines and ticks still need a live visit (plain 404). | Owner: "the assigned nurse". Widening the shared live-access check would have let a nurse edit a finished plan. The visit summary answers in the grace so the app can find the plan id after clock-out (`can_author` is false then). |
| **Documentation space is its own table `visit_documentation`**, not a flag on `booking_notes` or on care-plan observations. | `booking_notes` are shown to the patient; observations are shared with every caregiver on the client. The owner wants this private to the author and the console. Name note: `caregiver_documents` already exists (licence and ID uploads), so this one is `visit_documentation`. |
| **Documentation is append-only, encrypted (Fernet), keyed to the visit, optionally linked to the client's active plan.** Windows are the note windows: write in progress + 2 h; the author reads for 24 h after the visit ended (then 404); the admin console keeps reading. | Same as the visit notes and care-plan observations, so there is one mental model. Any caregiver on the visit may write, not only a nurse. |
| **Admin read is `GET /admin/visit-documentation` under `careplans.read`, audited as `visit_documentation.view` (filters and count only).** | The Clinical Reviewer already holds `careplans.read`; the audit trail matches how care-plan notes are read. Reading someone's private notes should leave a trace. |
| **Erasure:** a patient's erasure blanks the text of documentation written for their visits (rows stay), like plan notes. | Health text about that person. A caregiver's own erasure keeps what they wrote until retention, like visit notes. |

## What's involved
| Path | Change |
|---|---|
| `app/alembic/versions/0031_care_templates_docs_space.py` | the migration (text edits, flag, table) |
| `app/db/models/care_plan.py` | `CarePlanTemplate.caregiver_can_use`, `VisitDocumentation` |
| `app/services/care_plan_access.py` | `may_use_template`, `may_author_plan`, `require_author(plan=, template=)`, `resolve_grace_access_for_plan`, `resolve_grace_access_for_booking`, `post_visit_note_grace` |
| `app/services/care_plans.py` | capabilities (`can_edit_medications`), `observations_close_at`, `author_scope`, template list filter |
| `app/services/care_plan_observations.py` | `require_note_window` (in progress, or the nurse's grace) replaces the live-only check |
| `app/api/v1/endpoints/caregiver_care_plans.py` | role checks by plan or template; `note_grace` access for the plan read and the notes routes; visit summary grace |
| `app/services/visit_documentation.py`, `app/api/v1/endpoints/visit_documentation.py`, `app/schemas/visit_documentation.py` | the documentation space (caregiver routes and the admin read) |
| `app/services/account_erasure.py` | blanks documentation text for an erased patient |
| `app/schemas/care_plan.py` | new fields on templates, capabilities, plan and visit summary |
| `.github/workflows/ci.yml` | the new guide's render check |
| Monorepo | `apps/caregiver` (My documentation screen, Elderly template path, medicine gate, notes window line, post-visit links), `apps/admin` (Clinical > Caregiver documentation), `packages/api-client` (regenerated) |

## Verification
- Backend: `app/tests/test_care_templates_docs_space.py` (41 tests: text, flag, Elderly authoring, medicines refused, grace rules and edges, documentation windows, privacy, admin read and audit, erasure), `test_care_templates_migration.py` (round trip on a scratch DB, single head), and the care-plan suites whose expectations changed on purpose (`test_care_plans_access.py`, `test_care_plans_core.py`, `test_care_plans_migration.py`). Run against a local Postgres 17 because Docker was down on the build machine; CI runs the full suite.
- Live captures: `docs/fe-captures/care-templates-docs-space/` (27 files, `test_capture_care_templates_docs_space.py`), embedded in the guide and re-render-checked in CI.
- Apps: jest (`visitDocumentation`, `carePlanHub`, `carePlanNotesProgress`) and vitest (`documentation-list`); typecheck and eslint clean. **Not run on a simulator or phone: visual pass pending.**

## Operate / roll back
- Deploy: the migration runs with the normal release. No new settings. No data to backfill.
- Roll back the code: safe at any time, but the new routes disappear and the apps built from this PR will call missing routes.
- Roll back the migration: `alembic downgrade 0030_bank_name_review` restores the version 1 text and drops the column; **it also drops `visit_documentation` and every entry in it**, so do not downgrade once caregivers have written documentation. Plans already made from version 2 keep their own copy of the text.
- The two windows are existing settings: `VOICE_NOTE_POST_VISIT_GRACE_MINUTES` (120) and `VOICE_NOTE_EDIT_WINDOW_HOURS` (24).

## Follow-ups
- Clinical sign-off of the five templates in the admin API (no console screen yet); until then they read "Not yet clinically reviewed".
- Caregivers editing medications (with the Clinical/Nursing Supervisor step) and the admin medicines screen are a separate slice; `can_edit_medications` is the switch the apps already read.
- The grace is nurse-only by the owner's wording; extending it to any caregiver is `visit_windows` plus the access helper.
- Older captures of the care-plan guide (`docs/fe-captures/caregiver`, `admin`) predate the new fields; the guides carry a note. Re-capture when those guides are next touched.
- The `not_a_nurse` message ("Only nurses can create or change a care plan.") is also what a non-nurse gets for a medicine; the app hides the control instead of showing it.
- Caregiver app screens need a simulator pass; the admin page has not been opened against a live backend in a browser.
