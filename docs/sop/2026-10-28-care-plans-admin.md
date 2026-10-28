# SOP: Care plans, slice B4 (admin API)

| | |
|---|---|
| **Date** | 2026-10-28 |
| **Branch / PR** | `feat/care-plans-admin`, **stacked on** `feat/care-plans-medications` (B3, backend PR #31, `069b3a0`). Not pushed, no PR (the lead pushes). After #31 merges: `git rebase oakly/main`. |
| **Spec** | [`docs/plans/2026-10-27-caregiver-care-plans-design.md`](../plans/2026-10-27-caregiver-care-plans-design.md) section 14 (B4), 4.2, 6.2, 10, decision D1. |
| **Contract** | [`docs/fe-integration-guide-admin.md`](../fe-integration-guide-admin.md) section 12.13 (captures `500` to `513`). |
| **Migration** | `0019_care_plan_admin_permissions` (data only; the design calls it `0018`, B3 took it). |

## What shipped
The Clinical Reviewer (and Super Admin) can read, author and annotate care plans, and sign off the five seeded templates, through the admin API. **No console screens in v1**: the Clinical Reviewer authors through this API (D1; the design's non-goals).

| Permission | Holders | Routes |
|---|---|---|
| `careplans.read` | Clinical Reviewer, Super Admin | `GET /admin/care-plans`, `GET /admin/care-plans/{id}` (audited), `GET /admin/care-plans/{id}/observations` (audited), `GET /admin/care-plan-templates` |
| `careplans.write` | same | `POST /admin/care-plans`, `PATCH /admin/care-plans/{id}`, `POST .../observations`, goals (`POST .../goals`, `PATCH`/`DELETE /admin/care-plan-goals/{id}`), tasks (same shape), medications (`POST .../medications`, `PATCH /admin/care-plan-medications/{id}`, `POST .../{id}/stop`) |
| `careplans.templates` | same | `POST /admin/care-plan-templates/{id}/review` |

17 routes in `app/api/v1/endpoints/admin_care_plans.py`. **The design heading says "9"; its own table defines the scope as "the same operations as 10-18 and 22"** (goals, medications and tasks each add/edit/remove-or-stop, plus the note), which cannot fit in 9 typed routes, so the routes mirror the caregiver ones (4 plan + 3 goal + 3 task + 3 medication + 1 note + 2 template) plus one read of the notes (a reviewer who can write notes must be able to read them). Told to the lead.

## Why
Design 6.2: a clinician needs to author plans without a nurse's visit, and the go-live gate is "every seeded template reviewed or removed" ([design section 7](../plans/2026-10-27-caregiver-care-plans-design.md)): that needs a sign-off endpoint with a recorded reviewer.

## How (decisions and what was rejected)
| Question | Decision | Rejected |
|---|---|---|
| Reuse | The B1-B3 write services (`update_plan`, goals, tasks, medications, create) now take an `actor`: a `Caregiver` or `AdminActor(admin, request)`. **One choke point**, `care_plans.actor_event`, writes the `care_plan_events` row and, for an admin, the `admin_audit_log` row (`care_plan.<event>`) in the **same transaction** (the services commit once, after it). | A second admin service layer (duplicates the limits, the locking and the version rules); an audit decorator at the route (would commit separately from the change, or miss the events the service decides on, such as "no change"). |
| Create | `care_plans.create_plan_for_subject(db, actor, subject, booking_id, payload)` is the one create path; the caregiver wrapper keeps its signature. Admin create names the client with exactly one of `patient_id` / `care_recipient_id` (422 otherwise); a patient must be active (404 `client_not_found`); a care recipient has no active flag. `created_by_admin_id` is set, `created_from_booking_id` is null. | Admin creating through a booking id (an admin has no visit). |
| Admin plan shape | `AdminCarePlanOut` = the shared `plan_core` + `subject {kind,id}`; **no `booking_id`, no `capabilities`**. A separate response model, not a subclass, so the caregiver `CarePlanOut` (and the B1-B3 captures) are untouched. | Reusing `CarePlanOut` with fake values. |
| Audit content | `after` holds field names, ids and counts only (the `fields` the service already passed to the plan event). Finance reads this log (`audit.read`), so a test searches every audit row and event for every secret string of a full authoring session. | Before/after of values. |
| Reads | `GET` plan: audited `care_plan.view` (fields shown, like `booking.clinical_view`), committed before the response. Notes read: `care_plan.observations_view`. The list is **not** audited and **carries no plan text** (name, risk, status, counts, dates) so it can be browsed without an audit row per row. | Auditing the list (noise); a diagnosis in the list (health text without an audit row). |
| List | `limit`/`offset`/`total` like the other admin lists (the design said `page=`). `q` is the client's name only (the diagnosis is encrypted), LIKE-escaped. Counts come from three grouped queries, not per row. | `page=`; per-row counts. |
| Notes | Admin note: `author_admin_id`, no booking, plan must be active, does not bump the version (same as a nurse's), audited. | Requiring a visit. |
| Template review | `approved`/`pending`; always applied and audited (before/after status, key, version), re-stamping `reviewed_by_admin_id`/`reviewed_at`, so a second reviewer's sign-off is on record. Row-locked. No content edit exists, so a sign-off cannot go stale. | A no-op when the status is unchanged (loses the second sign-off). |
| Errors | Same codes as the caregiver routes; unknown plan/goal/task/medication id is the same plain `404 "Care plan not found"`. 404/409 responses carry `Cache-Control: no-store` through `HTTPException(headers=...)` (see Follow-ups: the caregiver routes do not). | |
| Client name | Shown in full under `careplans.read`, not masked: the Clinical Reviewer holds `clinical.read` and not `people.pii`, but a plan is identified by its client and the plan text is far more sensitive than the name. **A decision for the owner to confirm** (one line to mask). | Masking (the reviewer could not tell which client they are editing). |

## What's involved
- Migration `app/alembic/versions/0019_care_plan_admin_permissions.py`: inserts the three permissions for `clinical_reviewer` and `super_admin` (idempotent); the downgrade removes only those rows from those two roles. `app/core/admin_permissions.py` has the same catalogue (`test_admin_rbac` fails on drift).
- `app/api/v1/endpoints/admin_care_plans.py`, `app/api/v1/api.py` (mount `/admin`), `app/schemas/admin_care_plan.py`, `app/services/care_plan_admin.py`.
- Refactor: `app/services/care_plans.py` (`AdminActor`, `Actor`, `actor_event`, `plan_core`, `create_plan_for_subject`), `care_plan_tasks.py`, `care_plan_medications.py`, `care_plan_observations.py` (`add_admin_observation`). The caregiver behaviour is unchanged (the whole B1-B3 suite passes untouched, except the two test edits below).
- Tests: `test_admin_care_plans.py`, `test_admin_care_plans_audit.py`, `test_admin_care_plans_migration.py`, 17 rows in the `test_admin_rbac.py` matrix, capture script `test_capture_care_plans_admin.py` (skipped unless `CAPTURE_CARE_PLANS=1`). Changed: `test_care_plan_medications_migration.py` (0018 is no longer the head).
- Docs: admin FE guide section 12.13 (and the route table in section 6), this SOP, the master checklist.

## Verification
**Full local CI parity (2026-10-28):** `poetry check --lock`, `ruff check --select E9,F63,F7,F82 .`, `python -m scripts.ci_smoke` (CI's dummy env vars; 197 operations, up from 180), both `render_fe_guide --check`, and the full pytest under the shared lock: **3312 passed, 1 failed, 6 skipped** (33 min). The one failure was `test_admin_migration::test_roles_and_permissions_are_seeded` (the 0003 seed test did not know 0019 adds permissions); fixed, and that file plus `test_admin_rbac.py` and `test_migrations.py` re-run green (107 passed), so the suite is 3313 passed, 6 skipped. New: 41 tests in `test_admin_care_plans.py` and `test_admin_care_plans_audit.py`, 2 migration tests, 17 rows in the RBAC matrix (7 roles each), and the capture script (skipped unless `CAPTURE_CARE_PLANS=1`; captures `500` to `513` are real responses from the app on a real Postgres, test harness, not the compose stack).

Highlights: create for a patient, a recipient and from a template; one client id exactly; unknown/erased client (inactive patient, erased person's own recipient row); duplicate plan with the existing id; list filters, name search (LIKE-escaped), paging, text-free rows; open/patch/stale `409` carrying the admin-shaped plan; goal, task and medication writes through the shared services; completed plan refuses writes; notes (admin and nurse authors); the nurse sees the admin's plan and edits; template list and review (approve, take back, second sign-off recorded); every route against every role; one audit row per admin write in the same transaction (a failure after the audit row leaves neither), **no plan text in any audit row or event** across a full authoring session; refused writes leave nothing; Finance can read the trail and sees no plan text; 422s never echo values; `no-store` on every response including `404`/`409`; migration round trip and idempotence.

**Opus review (one pass, read-only):** REQUEST CHANGES, no HIGH, two MED, all fixed with tests, no re-review (the fixes are small and covered):
- MED: a stale-version `409` (its body is the current plan) and a write that changed nothing (empty `PATCH`, repeated remove/stop) returned plan text with no audit row. Now audited as `care_plan.view` with `after.via` `version_conflict` / `unchanged_write`.
- MED: an admin could create a plan for an erased person's own recipient row. Now `client_not_found` (the recipient named `Deleted user`).
- LOW fixed: `actor_event` refuses anything but a `Caregiver` or `AdminActor` (`TypeError`); the list batches author names (no query per row).
- LOW left: the unchanged-write check compares the plan version before and after, so a concurrent writer between the two could hide an unchanged-write read (see Follow-ups); `TemplateReviewIn` has no template version (a sign-off is tied to the content only through the audit row's recorded version; no template edit exists in v1); the list shows client name, age and gender unaudited (design 6.2).

## Operate / roll back
Deploy: `alembic upgrade head` (`0019` after `0018`). No config, no new table. Give an admin the Clinical Reviewer role (admin invite/role routes, guide sections 7-8) for them to use the routes. Roll back by reverting and `alembic downgrade 0018_care_plan_medications`: removes the three permissions from the two roles; plans and audit rows the admins wrote stay. Admin-authored plans carry `created_by_admin_id`, so reverting code while such plans exist is safe (the caregiver code already handles `author.kind: "admin"`).

## Follow-ups
- **B1-B3 (caregiver routes): 404 and 409 responses lack `Cache-Control: no-store`.** The router's dependency header is dropped when an `HTTPException` is raised; the `409 version_conflict` body carries the whole plan (health text). Fix: pass `headers={"Cache-Control": "no-store"}` in `caregiver_care_plans._refuse`/`_conflict` (as `admin_care_plans.py` does) and re-capture. Found by the B4 no-store test; not changed here (outside this slice).
- Console screens for plans and templates, template create/edit (design section 15).
- An admin client search/picker route: the console has to find `patient_id` / `care_recipient_id` from the booking screens today.
- Paging the list by `page` (design wording) is `limit`/`offset` here, like the other admin lists.
- The audit log has no event history of a plan for the reviewer's screen: `care_plan_events` is not exposed by any route.
- Admin read of progress and the day view are not exposed (the Clinical Reviewer reads and authors the plan; ticks are the caregivers').
- Template review could take the template `version` the reviewer read and refuse a mismatch (relevant once template editing exists).
- The "unchanged write is a read" audit compares the plan version before and after the call; a concurrent writer in between could suppress that one audit row. A service-level flag ("this call recorded an event") would remove the race.
- Decide masking of the client name for the Clinical Reviewer (above).
