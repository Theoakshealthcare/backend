# SOP: Care plans, slice B1 (foundation)

| | |
|---|---|
| **Date** | 2026-10-27 |
| **Branch / PR** | `feat/care-plans-core`, **stacked on** `feat/kyc-foundation` (K1, migration `0015_caregiver_kyc`). Not pushed, no PR (the lead pushes). |
| **Spec** | [`docs/plans/2026-10-27-caregiver-care-plans-design.md`](../plans/2026-10-27-caregiver-care-plans-design.md) section 14, slice B1. Owner decisions D1 to D6 and assumptions A1 to A12 are accepted as written. |
| **Contract** | [`docs/fe-integration-guide-caregiver.md`](../fe-integration-guide-caregiver.md) section 16 (captures `200` to `222`). |
| **Migration** | `0016_care_plans_core` (the design calls it `0015`; K1 took that number). |

## What shipped
A nurse records a care plan for a client; every caregiver on a live visit for that client can read it. B1 is the plan itself: the subject and access resolver, the nurse test (and `can_author_care_plans` on `GET /caregiver/home`), five seeded templates, plans with goals and task definitions, an event trail, and eleven routes. Ticks, observations, medications, the admin API and the mobile screens are B2 to B4 and M1 to M3 and are **not** built.

| Route | Who |
|---|---|
| `GET /caregiver/home` (new field `can_author_care_plans`) | any caregiver |
| `GET /caregiver/care-plan-templates`, `GET .../{id}` | nurse |
| `GET /caregiver/care-plans?status=&q=` | any caregiver (clients of my live visits) |
| `GET /caregiver/care-plans/candidates` | nurse |
| `GET /caregiver/bookings/{booking_id}/care-plan` | any caregiver |
| `POST /caregiver/bookings/{booking_id}/care-plan` | nurse |
| `GET /caregiver/care-plans/{plan_id}` | any caregiver with access |
| `PATCH /caregiver/care-plans/{plan_id}` (header, allergies, special instructions, `completed`/`archived`) | nurse |
| `POST /caregiver/care-plans/{plan_id}/goals`, `PATCH` and `DELETE /caregiver/care-plan-goals/{id}` | nurse |

## Why
Gap analysis section 5 and owner decisions of 2026-10-27: nurses create and edit plans, other caregivers view them, the plan follows the client (not the booking) and is visible only while a caregiver is assigned, health text is encrypted at rest.

## How (decisions and what was rejected)
| Question | Decision | Rejected |
|---|---|---|
| Whose plan | The **subject**: `patient_id` for a visit booked for oneself (`myself`, any case), `care_recipient_id` for a booking made for someone else, **none** for an app booking for someone else (`subject_ready: false`, create is `409 client_not_identified`). Copies the client profile's `myself` rule exactly (A1). | One key "patient or recipient": the booking's `patient_id` is the **booker** for someone else's visit. |
| Access | The client profile's rule unchanged (own booking, `accepted`/`en_route`/`in_progress`, suspended caregiver only `in_progress`, patient active), by booking id; for plan-id and goal-id routes the plan's subject must equal the subject of one of the caller's live bookings (nearest used). One 404 `"Care plan not found"` for everything. A test asserts the visit summary and the profile agree over 56 status/suspension/active/ownership combinations. | Patient grants (`PatientCaregiverAccess`): the profile does not use them (D5). |
| Order of checks | Access first (404), then role (403 `not_a_nurse`), so a non-nurse stranger learns nothing. | 403 first (leaks plan existence). |
| Nurse test | A `caregiver_skills` row with `service_type = 'nurse'` (`CARE_PLAN_AUTHOR_SERVICE_TYPES`, A5). The typed `specialization` never counts (tested). | Using `specialization` (free text). |
| Encryption | Fernet via `encrypt`/`decrypt`, explicit in the service. Diagnosis, allergies and special instructions (encrypted JSON lists), goal title and target, task title. **No plaintext fallback**: a decrypt failure is a 500 with a log line naming the plan id and column, never the value. The `*_encrypted` columns are nullable only so erasure can wipe them; the service refuses to write a plan without them. | The `PatientVital` property pattern that returns plaintext on a decrypt error. |
| Concurrency | `version` on the plan, bumped by every plan or goal write, checked under a row lock (`409 version_conflict` returns the current plan). One active plan per client by partial unique indexes; a create race maps the `IntegrityError` to `409 care_plan_exists`. `expected_version` is required on PATCH plan, PATCH/DELETE goal and optional on adding a goal (adding never overwrites). | Optimistic check without a lock. |
| Errors | `{"detail": {"code", "message"}}` (like the caregiver approval `unmet` items); the no-access 404 stays a plain string. | |
| 422 | FastAPI's 422 echoes the rejected value (`input`). On care plan routes that is health text, so a route class strips `input` (design 4.3: errors name the field, not the value). Tested with a long diagnosis. | A global change (it would alter every other endpoint's shape). |
| Templates | Five drafts seeded by the migration (`ON CONFLICT (key) DO NOTHING`) as `clinical_review_status = 'pending'`: routine tasks and goal wording, no medicines, doses or numeric thresholds (tested). The test harness re-inserts them after each truncate (`TRUNCATE admin_users CASCADE` reaches the table). | Hiding pending templates (A10). |
| Create from a template | `template_id` only fills what the body leaves out; the plan is a copy (`template_version` recorded, later template edits never reach it). | A live reference to the template. |
| Goals | `DELETE` marks the goal `discontinued` and keeps it; reads list it with its status. At most 30 not discontinued. | Hard delete. |
| Events | `plan.create`, `template.use`, `plan.update`, `plan.status`, `goal.add/update/remove`, `plan.erased`; `detail` holds field names and ids only (tested against every secret string). | Copying health text into history. |
| Family view | Groundwork only (D2): `family_visible boolean not null default false` on goals and tasks (no request or response carries it), and `resolve_family_access(db, user, plan_id)` over `care_relationships` (role family or guardian, `clinical_access`), the latest `share_with_family` consent for the recipient, an active user and a live plan; it returns only the flagged items, never the header. `patient_id` plans are refused until the family view settles A12. A test asserts no route calls it. | Exposing any family route. |
| Erasure | `AccountErasureService` archives the plans of the erased patient and of their `self` recipient row and nulls every ciphertext (header, goals, tasks); ids, enums, dates and events stay, plus a `plan.erased` system event. Plans of recipients they merely booked for are other people's data and are left. | Hard delete (plans are never deleted). |

## What's involved
- Migration `app/alembic/versions/0016_care_plans_core.py` (five tables, constraints, partial unique indexes, seeds, downgrade drops the five).
- Models `app/db/models/care_plan.py`; schemas `app/schemas/care_plan.py`, `app/schemas/caregiver_app.py` (Home field).
- Access `app/services/care_plan_access.py`; logic `app/services/care_plans.py`; routes `app/api/v1/endpoints/caregiver_care_plans.py`; mount in `app/api/v1/api.py`; Home `app/services/caregiver_home.py`.
- Erasure `app/services/account_erasure.py` (`_wipe_care_plans`); rotation `scripts/reencrypt_health_data.py` (six columns; a test asserts every `*_encrypted` column on the care plan models is listed).
- Test harness `app/tests/conftest.py` (re-seeds `care_plan_templates`); `app/tests/test_kyc_migration.py` (K1's "0015 is the head" assertion became "0015 follows 0014", because 0016 is now the head; single head is asserted by `test_migrations.py`).
- Tests (this slice): `test_care_plans_core.py`, `test_care_plans_access.py`, `test_care_plans_security.py`, `test_care_plans_migration.py`; capture script `test_capture_care_plans.py` (skipped unless `CAPTURE_CARE_PLANS=1`).

## Verification
See the report for counts. Highlights: the access matrix (every route x each non-live status, other caregiver, missing id, dropped out, erased client, suspended caregiver, patient token 403, anonymous 401) returns the identical 404 body with no plan text; the profile-agreement matrix; subject branches (myself, `Myself`, WhatsApp relative, app booking for someone else); non-nurse 403 on every authoring route even with a self-declared "nurse"; ciphertext in the raw columns and round trip; wrong key and plaintext-in-column are 500s with a safe log; rotation reaches the six columns; erasure leaves no ciphertext; `family_visible` cannot be set by any body and defaults false in the database; the family check's allow and refuse cases; migration round trip, constraints and seeds. Captures `200` to `222` are real responses from the app on a real Postgres via the test harness (not the compose stack: RAM rule).

**Opus review:** see "Review" below.

## Operate / roll back
Deploy: `alembic upgrade head` (`0016` after K1's `0015`). No config. **Go-live gates:** a clinician reviews or removes every seeded template (all are `pending`); at least one caregiver needs the `nurse` skill (admin-assigned) to create plans; a device pass (none yet). Roll back by reverting the commit and `alembic downgrade 0015_caregiver_kyc`, which drops the five tables and any plans in them; do not downgrade once real plans exist.

## Follow-ups
- **Later slices move up one revision** because K1 took `0015`: B2 `0017_care_plan_ticks_and_notes`, B3 `0018_care_plan_medications`, B4 `0019_care_plan_admin_permissions`. The design doc still says `0016` to `0018`.
- B2 adds `capabilities.can_tick_tasks` / `can_add_observation`, `progress_percent` on the assigned list, and the task add/edit/remove routes; B3 adds `medications` to the plan. They are absent from B1 on purpose (a flag for a route that does not exist would mislead the app). Both extend `scripts/reencrypt_health_data.py` and the erasure hook.
- `family_visible` on observations arrives with B2. The admin API (B4) must use the same service functions and write `AuditService` rows with field names only.
- A plan has no way to be reopened or to go from `completed` to `archived`; a nurse corrects by creating a new plan.
- Allergies exist in two places (the patient record and the plan); not merged in v1 (assumption A11).
- Known weakness inherited from the design: a person who books through both channels can end up with a `patient_id` plan and a `care_recipient_id` plan.
- Retention for plans beyond erasure is undefined (A9).
- K1 owns `app/tests/test_kyc_migration.py`; if it changes that head assertion again, take theirs and keep it free of "is the head".

## Review
**Opus (one read-only pass, no re-review needed):** APPROVE WITH FIXES. No access-control, oracle, encryption, erasure or `family_visible` defect. Both resolvers apply the profile's rule; booker and relative cannot be confused; access is always checked before the 403 and the 409s; goal routes go goal, plan, access. Fixed from it: (1) **medium** the 422 echoed rejected health text (`input`), including the whole body for a model-level error; now stripped on care plan routes, with tests for both cases; (2) reactivating a discontinued goal bypassed the 30-goal cap; (3) the assigned list's name search ran after the 100-plan cap; (4) the latest-consent lookup now has an id tiebreak.

Accepted, as follow-ups:
- `GET /care-plans/{id}` answers a missing plan slightly faster than an existing plan with no access (weak timing oracle; plan ids are random UUIDv4). Fix by always running the live-booking query.
- Access is checked before the plan row is locked and the booking is not locked, so a booking cancelled in that millisecond gap lets one write through. Re-check after `_lock_editable` if it ever matters.
- Older than this slice: after a person with a `self` recipient row is erased, a relative's live booking for that recipient still gives a nurse access, so a new plan can be created for the anonymised person. Decide whether erasure should cancel or block those bookings.
