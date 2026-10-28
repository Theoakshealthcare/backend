# SOP: Care plans, slice B2 (ticks, notes, progress)

| | |
|---|---|
| **Date** | 2026-10-28 |
| **Branch / PR** | `feat/care-plans-tasks`, **stacked on** `feat/care-plans-core` (B1, backend PR #26, head `37c62ae`). Not pushed, no PR (the lead pushes); rebase onto `oakly/main` once #26 merges. |
| **Spec** | [`docs/plans/2026-10-27-caregiver-care-plans-design.md`](../plans/2026-10-27-caregiver-care-plans-design.md) section 14 (B2), 5.5, 5.7, 6.1 routes 16-23. Assumptions A4, A6, A8 apply as written. |
| **Contract** | [`docs/fe-integration-guide-caregiver.md`](../fe-integration-guide-caregiver.md) sections 16.13 to 16.20 (captures `230` to `238`). |
| **Migration** | `0017_care_plan_ticks_and_notes` (design calls it `0016`; KYC took `0015`, B1 `0016`). |

## What shipped
The daily work on a care plan: nurses add, edit and remove tasks; every caregiver on the client sees the day view, ticks tasks during their own in-progress visit, leaves visit notes and sees progress. Medications (B3), the admin API (B4) and the mobile screens are **not** built.

| Route | Who |
|---|---|
| `POST /caregiver/care-plans/{plan_id}/tasks`, `PATCH` and `DELETE /caregiver/care-plan-tasks/{id}` | nurse |
| `GET /caregiver/care-plans/{plan_id}/tasks?date=` (day view) | any caregiver with access |
| `PUT /caregiver/care-plan-tasks/{id}/completion` `{date, done}` | any caregiver, own visit in progress, today only |
| `GET` and `POST /caregiver/care-plans/{plan_id}/observations` | read: any with access; write: any, own visit in progress |
| `GET /caregiver/care-plans/{plan_id}/progress` | any with access |

Also: `capabilities` on the plan gained `can_tick_tasks` and `can_add_observation`; the assigned list gained `progress_percent`.

## Why
Design section 14: the plan is only useful if the person at the bedside can work from it and the team can see what was done. Ticks and notes are the part several caregivers write at once, so the slice is mostly about getting concurrency and "today" right.

## How (decisions and what was rejected)
| Question | Decision | Rejected |
|---|---|---|
| Tick storage | `care_plan_task_completions`, `UNIQUE (task_id, for_date)`. Tick = `INSERT ... ON CONFLICT DO NOTHING`; untick = `DELETE ... RETURNING`; both idempotent and answer the current state. Untick writes a `task.untick` event (ids and date only); a tick is its own record. | Read-then-write (races); a `done` boolean row (loses who/when on untick). |
| Races | A tick takes `FOR SHARE` on the plan row and on the caller's booking, then re-reads their status. Plan editors take `FOR UPDATE`, so a tick cannot land on a plan completed a moment earlier, and a visit that just ended refuses it. Ticks do not block each other. Tested with two real sessions (a tick waits for a plan being completed, then is refused) and 8 concurrent ticks. | Trusting the access check made earlier in the request. |
| "Today" | `local_today()` (Africa/Lagos) read **after** the locks, compared with the body's date; any other date is 422 `task_date_not_today`. Tested at 22:59:59 / 23:00:00 UTC and year end, and with a clock moved past midnight between two calls (A6). | Using the client's date or the server's UTC date. |
| Who ticks | Any caregiver with a live visit **of their own in progress** (not only a nurse). `accepted`/`en_route` is 409 `visit_not_in_progress`. A completed/archived plan is 409 `plan_not_editable`. A task not due that day (weekday, plan dates, added later) is 422 `task_not_scheduled`; a removed task is the plain 404. | |
| Version | Ticks and notes **do not bump `plan.version`** (they overwrite nothing; bumping would make every tick a `version_conflict` for a nurse editing). Task add/edit/remove do, with the same `expected_version` rules as goals (optional on add, required on PATCH, query param on DELETE). | Bumping on every write (design 3.3 says "every plan or child write"; ticks and notes are the exception, see deviations). |
| Task removal | `is_active = false`; the row and its ticks stay. Idempotent on repeat. 60 live tasks max. | Hard delete. |
| Scheduling rule | One predicate shared by the day view, ticking and progress: active, inside `start_date..start_date+duration-1`, weekday in `days_of_week`, and **not before the day the task was added** (a task added today was never due last week). | Showing a new task on past days of an older plan. |
| Observations | Append-only (no PATCH/PUT/DELETE route exists, tested; no trigger, because erasure blanks the body). Body 1-2000 chars, Fernet at rest, no plaintext fallback. `category` `observation` or `special_note`. Any assigned caregiver may write (A4). Responses carry author name and kind, never booking ids. Keyset paging newest first on `(created_at, id)`: `before` + `before_id`. | Nurse-only (A4 is one constant); a timestamp-only cursor (skips ties). |
| `family_visible` | `observations.family_visible` defaults false at the database; it is in no request or response model (a body that sends it is ignored, tested). `resolve_family_access` now also returns flagged observations; still called by no route. | |
| Progress | Ticked / scheduled occurrences counted **only over visit days** (A8). A visit day is any day a visit for the same subject (any caregiver) was under way: from `actual_start_time` (scheduled date if never stamped) to its end, so a visit past midnight counts both days; a visit with no end counts at most 24 h. The window is plan start to min(today, last plan day, the day the plan was completed or archived). Removed tasks leave both sides. No visit or nothing scheduled: `overall_percent: null`. `by_day` = last 14 visit days; `day` is clamped to `0..of_days`. | Counting every calendar day (a Mon/Wed/Fri nurse would always look 57% behind). |
| Erasure | `wipe_plans_for_subject` also nulls every observation body. Ticks stay (ids and dates only). | Deleting the ticks. |
| Rotation | `care_plan_observations.body_encrypted` added to `reencrypt_health_data.COLUMNS`; the introspection test now finds **every table named `care_plan*`** itself, so B3's table cannot be forgotten. | The fixed table tuple. |

## What's involved
- Migration `app/alembic/versions/0017_care_plan_ticks_and_notes.py` (two tables, unique and check constraints, one index; downgrade drops both).
- Models `app/db/models/care_plan.py` (`CarePlanTaskCompletion`, `CarePlanObservation`); schemas `app/schemas/care_plan.py`.
- Services `app/services/care_plan_tasks.py` (task edits, day view, ticks, progress), `app/services/care_plan_observations.py`; `app/services/care_plans.py` (errors, capabilities, erasure); `app/services/care_plan_access.py` (family access returns observations).
- Routes `app/api/v1/endpoints/caregiver_care_plans.py` (same router, same 404 and 422-scrub rules as B1); `scripts/reencrypt_health_data.py`.
- Tests: `test_care_plan_tasks.py`, `test_care_plan_observations.py`, `test_care_plan_progress.py`, `test_care_plans_b2_access.py` (every new route x non-live statuses, strangers, dropped out, erased, suspended, patient 403, anonymous 401, role matrix), `test_care_plans_b2_security.py`, `test_care_plan_ticks_migration.py`; B1 tests updated for the three `capabilities` flags and `progress_percent`; capture script `test_capture_care_plans_b2.py`.

## Verification
See the report for counts. Highlights: idempotent tick/untick by one and by different callers; one event per real untick; 8 callers ticking at once leave one row; a tick waits on a plan being completed and is then refused; a visit that ended after access was resolved refuses the write; today-only around Lagos midnight; day-view and progress rules (weekday, window, added-later, removed, visit days, Lagos date, past-midnight visit, closed plan); notes encrypted at rest, rotated, wiped by erasure while ticks stay; no edit/delete route for notes; the flag cannot be set; closed-list response shapes. Captures `230` to `238` are real responses from the app on a real Postgres via the test harness; progress history was seeded by direct inserts (the API cannot tick a past day).

**Opus review (one pass):** APPROVE WITH FIXES, no HIGH or MED. Access, locks, midnight, encryption/erasure and the family flag were clean. Fixed: (1) a visit running past midnight counted only its start day, so ticks made after midnight never counted; (2) a closed plan kept accumulating visit days, so its percentage fell; (3) a timestamp-only notes cursor skipped notes with equal timestamps (now `before_id`). 

## Operate / roll back
Deploy: `alembic upgrade head` (`0017` after `0016`). No config. Roll back by reverting and `alembic downgrade 0016_care_plans_core`, which drops every tick and note; do not downgrade once visits have been recorded.

## Deviations from the design doc
- Migration number `0017` (see above).
- Ticks and notes do not bump `plan.version` (doc 3.3 says every child write).
- `capabilities` and the assigned list's `progress_percent` follow the doc's sketch; B1 had left them out until the routes existed.
- Visit days extend A8: a visit past midnight counts both days (ticks are allowed on both), and the window stops when the plan is closed.
- A task is not shown or counted on days before it was added (the doc's 5.5 says only "within the plan's dates"); the same rule is used by progress per 5.7.
- Extra codes: 422 `task_not_scheduled`. Notes list also takes `category` and `before_id`.

## Follow-ups
- A tick locks plan then booking; patient erasure locks the booking then the plan (Opus, LOW). A tick in flight while an unpaid in-progress booking is erased can deadlock; Postgres aborts one side (a 500, no data harm). Align the lock order if erasure ever runs during visits.
- The day view for a **past** day hides a removed task together with its kept ticks, so history changes when a task is removed. Decide whether past days should show tasks that were ticked.
- Editing a task's days or slot changes every past day too (one definition per task); no per-day snapshot.
- `before` without `before_id` still skips same-timestamp notes; the app should always send both.
- Admin note authoring (B4) must set `author_admin_id`; `ObservationOut` already renders admin authors.
- Retention of ticks and notes beyond erasure is undefined (A9).
