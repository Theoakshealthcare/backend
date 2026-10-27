# Caregiver care plans: design and slice plan

| | |
|---|---|
| **Date** | 2026-10-27 |
| **Status** | Design for build. The six product decisions in section 1 are final (owner, 2026-10-27, "yes to the defaults"); everything marked **Assumption** is mine and needs the owner's eye. |
| **Sources** | Figma exports `ss/caregiver/` frames `8.20.26`, `8.22.56`, `8.23.17`, `8.23.28`, `8.23.45`, `8.23.57`; [gap analysis](2026-10-06-caregiver-design-gap-analysis.md) section 5; the code at `oakly/main` `94f45b1` (cited as `file:line`). |
| **Scope** | Docs only. No code, no migration, no endpoint exists yet. |
| **API shapes** | Proposals, **not captured responses**. The FE guide for each slice is generated from live captures in that slice (the project rule); nothing here may be pasted into the guide. |

## 1. Goals, decisions and non-goals

**Goal.** A nurse records a care plan for a client (diagnosis, goals, medications, daily tasks, allergies, special instructions). Every caregiver assigned to that client sees it during their visit, ticks the day's tasks and leaves observations. The plan stays with the client across visits and caregivers.

**Final decisions (do not re-ask):**

| # | Decision |
|---|---|
| D1 | Caregivers with the `nurse` skill, and a Clinical Reviewer (admin role), create and edit plans. Other caregivers view and tick tasks. |
| D2 | No family view in v1 (later through the W4 web companion). The data is made family-ready now (section 4.4). |
| D3 | 4-5 templates are seeded in a table now. Admin screens for templates come later. |
| D4 | Medications are record-only, with a clear "not clinical advice" note, no dose alerts, no reminders. |
| D5 | A plan belongs to the client and persists across visits and caregivers. A caregiver sees it only while assigned, by the same rule as the client profile (backend #22, monorepo #9). |
| D6 | Notes, medications and allergies are encrypted at rest like vitals (Fernet). Call recording stays off. |

**Non-goals (v1):** family or patient access (no endpoint, no screen; only the data groundwork in 4.4); dose or interaction checks, medication reminders or adherence tracking; admin console screens for plans or templates (the admin **API** is in scope, section 6.2); FallGuard or vitals thresholds ("Critical" chips); push notifications about plan changes; per-visit care schedules (the "Care Schedule" row in frame `8.20.26` is read from the booking, not stored on the plan); photos or attachments; export or PDF.

## 2. The code today (builder check)

| Fact | Where | Consequence |
|---|---|---|
| `care.py` has only `CareRecipient` and `CareRelationship`. No plan tables. | `app/db/models/care.py:16-48` | All tables below are new. |
| Alembic head is `0014_refunds_v2`; migrations live in `app/alembic/versions/`, hand-written, with a long docstring, idempotent data steps and a documented downgrade. | `app/alembic/versions/0014_refunds_v2.py:1-45`, `0013_finance_audit_read.py:14-28` | Plan adds `0015`-`0018` (section 14). |
| **Access rule of the client profile:** the booking must be the caller's (`Booking.caregiver_id == caregiver.id`), status `accepted`, `en_route` or `in_progress`; a caregiver whose `profile_status != "approved"` only for `in_progress`; the patient must be active; every refusal is one 404 `"Client not found"`; the key is the **booking id**. | `app/services/caregiver_clients.py:22,83-95`, `app/api/v1/endpoints/caregiver_clients.py:20-41`, SOP `2026-10-27-caregiver-client-profile.md` ("How") | Care plans reuse this check verbatim (section 4) and add a stricter one for writes. |
| **Who the "client" is is not one table.** For `booking_for == "myself"` the client is the `Patient`. Otherwise the `Patient` row is the **booker**, and the client is the `CareRecipient`, which exists only on WhatsApp bookings. An app booking for someone else has **no identifiable client** (the profile shows `"Client"`). | `app/services/caregiver_clients.py:97-106`; `app/db/models/booking.py:76-79`; `app/schemas/caregiver_client.py:47-49` | **Contradicts D5's "the patient/care recipient" as a single key.** See 3.2 (subject) and section 11 (contradictions). |
| Skills are assigned by admins, checked against `SERVICE_TYPES` (`nurse`, `doctor`, `physiotherapist`, `caregiver`, `nutritionist`). | `app/db/models/caregiver.py:112-140`; `app/services/caregiver_approval.py:306-320` | The nurse test is a `caregiver_skills` row with `service_type='nurse'`. `specialization` (free text the caregiver typed, `caregiver.py:31`, `schemas/caregiver.py:69`) must **not** be used. |
| **Skills never reach the caregiver app.** `CaregiverProfile` has `specialization` but no skills; `CaregiverHome` has none. | `app/schemas/caregiver.py:58-80`; `app/schemas/caregiver_app.py:46-57` | The app cannot know it is looking at a nurse. A new boolean is needed (6.1, `can_author_care_plans`). |
| Clinical Reviewer is an **admin role**, with `clinical.read` and no plan permission. Admin checks are `require_permission("x.y")`; permissions live in `ALL_PERMISSIONS` / `ROLE_PERMISSIONS` **and** in the `admin_role_permissions` table (a migration inserts new ones). | `app/core/admin_permissions.py:19-28,33-98`; `app/api/deps.py:162-176`; `0013_finance_audit_read.py:21-28` | "Clinical Reviewer edits plans" means an **admin API** and new permissions, not the caregiver app. |
| Vitals encryption: `encrypt` / `decrypt` over a `MultiFernet` built from `VITALS_ENCRYPTION_KEY` (comma-separated, first key encrypts, all decrypt). `PatientVital` wraps columns in properties **and falls back to plaintext on a decrypt error**. `CareRecipient.notes_encrypted` and `Booking.condition_encrypted` use plain `*_encrypted` Text columns with explicit calls. | `app/core/security.py:26-46`; `app/db/models/patient_vital.py:30-55`; `app/db/models/care.py:28`; `app/db/models/booking.py:80-82` | Use the explicit `*_encrypted` column pattern, **without** the plaintext fallback (3.4). |
| Key rotation re-encrypts a fixed column list. | `scripts/reencrypt_health_data.py:36-42` | New encrypted columns must be added to that list in the same PR. |
| Erasure anonymises the patient and the `self` recipient row but keeps bookings, vitals, etc. | `app/services/account_erasure.py:352-365`, `:4` | Plan content for an erased subject must be wiped too (3.6). |
| Admin audit log is append-only, written in the caller's transaction; admin reads of clinical text are themselves audited (`booking.clinical_view`). | `app/services/audit.py:36-60`; `app/db/models/audit.py:10-20`; `app/services/admin_bookings.py:363-420` | Admin plan reads and writes use `AuditService`. |
| `HOME_TIMEZONE = "Africa/Lagos"` defines "today" for caregivers. | `app/core/config.py:105`; `app/services/caregiver_home.py:61` | Task ticks use this zone for "today". |
| Caregiver endpoints are mounted under `/caregiver` with `Depends(get_current_caregiver)` (a patient token is 403). | `app/api/v1/api.py:8-10`; `app/api/deps.py:76-85` | New router `caregiver_care_plans.py`, same mount. |
| OpenAPI to the monorepo: `OAKLY_BACKEND_DIR=<backend> pnpm api:sync` dumps the spec with the backend's Poetry env into `packages/api-client/openapi.json` and regenerates `src/schema.d.ts`; CI checks drift. | `caregiver/packages/api-client/scripts/sync-openapi.mjs:1-40`, `package.json` (`sync`, `generate`) | Mobile slices commit the regenerated spec. |
| Tests: pytest with testcontainers Postgres; factories `make_booking`, `make_caregiver`, `make_patient`, `make_admin`, `auth_headers`; one test file per feature, plus a migration test per migration. | `app/tests/factories.py`; `test_caregiver_client_profile.py`; `test_refunds_migration.py` | Section 9. |
| The app has **no** care plan code: no Home card, no ClientProfile plan row. Screens live in `apps/caregiver/src/screens/<area>/`, routes in `StackNavigator.tsx` (ClientProfile at `:134`), query keys in `src/api/keys.ts` (`clientProfile` exists). | `caregiver/apps/caregiver/src/navigation/StackNavigator.tsx:134`; `src/api/keys.ts:1-20` | Mobile slices add a `screens/careplan/` folder. |

## 3. Data model

### 3.1 Rules for every table

- Plain Alembic migrations in the `0014` style (docstring, downgrade that drops only what the migration added).
- **Encryption rule (one sentence):** every free-text column that describes a person is `*_encrypted` (Fernet via `app.core.security`); enums, times, dates and ids stay plaintext so they can be filtered and indexed.
- Timestamps are naive UTC (`datetime.utcnow`), as in the rest of the models.
- Plans are never hard-deleted. Children are soft-removed by status.

### 3.2 The subject (whose plan it is)

`care_plans` has two nullable foreign keys and a CHECK that exactly one is set:

| Booking | Subject |
|---|---|
| `booking_for` is `myself` (any case) | `patient_id` = `booking.patient_id` |
| otherwise, and `booking.care_recipient_id` is set (WhatsApp) | `care_recipient_id` |
| otherwise (an **app** booking for someone else) | **none: care plans are unavailable.** `GET` answers `plan: null, subject_ready: false`; create answers `409 client_not_identified`. |

This mirrors `caregiver_clients.py:97-106` exactly, so the plan and the profile always talk about the same person. **Assumption A1:** v1 does not create `CareRecipient` rows on the fly for app bookings. The patient app is paused and patients book on WhatsApp, where a recipient always exists, so the gap is small. The alternative (the nurse types the client's name and we create a recipient plus relationship plus link it to the booking) touches `bookings`, `care_relationships` and erasure, and belongs in its own change.

Known weakness (same as vitals, `docs/sop/2026-10-27-caregiver-client-profile.md` Follow-ups): one person can have both a `patient_id` plan (app, self) and a `care_recipient_id` plan (WhatsApp, self) because the self case has a recipient row too. The rule above always picks `patient_id` for `myself`, so within one booking path this cannot happen; across a person who uses both channels it can. Accepted for v1; a later merge job can fix it.

### 3.3 Tables

**`care_plan_templates`** (no patient data, nothing encrypted)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `key` | text unique | slug, e.g. `stroke_recovery` |
| `name`, `summary` | text | list card |
| `duration_days` | int | e.g. 30 |
| `review_every_days` | int | e.g. 14 |
| `default_risk_level` | text | `low`/`medium`/`high` |
| `body` | jsonb | `{"goals":[{"title","target","frequency"}],"tasks":[{"title","time_slot","time_of_day","days_of_week"}],"special_instructions":[text]}`. **No medications, no doses, no numeric clinical thresholds** (D4, liability). |
| `version` | int default 1 | bumped on content change; plans store the version they copied |
| `is_active` | bool default true | |
| `clinical_review_status` | text, CHECK `pending`/`approved`, default `pending` | see 7 |
| `reviewed_by_admin_id`, `reviewed_at` | fk admin_users, timestamp, null | |
| `created_at`, `updated_at` | timestamps | |

**`care_plans`**

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `patient_id` | fk patients, null | subject, see 3.2 |
| `care_recipient_id` | fk care_recipients, null | subject; `CHECK (num_nonnulls(patient_id, care_recipient_id) = 1)` |
| `template_id`, `template_version` | fk, int, null | provenance only; the plan is a **copy**, later template edits never change it |
| `status` | text CHECK `active`/`completed`/`archived`, default `active` | |
| `risk_level` | text CHECK `low`/`medium`/`high` | recorded by the author, never computed |
| `diagnosis_encrypted` | text not null | "Hypertension, Early Dementia" |
| `allergies_encrypted` | text null | encrypted JSON array of strings |
| `special_instructions_encrypted` | text null | encrypted JSON array of strings |
| `start_date` | date | |
| `duration_days` | int, CHECK 1..365 | |
| `review_every_days` | int null, CHECK 7..90 | |
| `created_by_caregiver_id` / `created_by_admin_id` | fk, null | `CHECK (num_nonnulls(...) = 1)` |
| `created_from_booking_id` | fk bookings, null | where it was created |
| `version` | int not null default 1 | optimistic concurrency, bumped on every plan or child write |
| `completed_at`, `archived_at`, `created_at`, `updated_at` | | |

Indexes: **partial unique** `(patient_id) WHERE status='active' AND patient_id IS NOT NULL` and the same for `care_recipient_id` (one active plan per client; a race gives an `IntegrityError` mapped to `409 care_plan_exists`); `(created_by_caregiver_id)`; `(status, updated_at DESC)` for the admin list.

**`care_plan_goals`**: `id`, `plan_id` fk, `title_encrypted` not null, `target_encrypted` null, `frequency` CHECK `daily`/`weekly`/`ongoing`/`one_time`, `status` CHECK `active`/`achieved`/`discontinued` default `active`, `sort_order` int, `family_visible` bool not null default false, `created_at`, `updated_at`. Index `(plan_id, sort_order)`.

**`care_plan_tasks`**: `id`, `plan_id` fk, `title_encrypted` not null, `time_slot` CHECK `morning`/`afternoon`/`evening`/`night`, `time_of_day` time null (display, e.g. 08:00), `days_of_week` smallint[] not null (1 = Mon .. 7 = Sun; "Daily" = all seven), `is_active` bool default true, `sort_order`, `family_visible` bool not null default false, `created_at`, `updated_at`. Index `(plan_id, is_active)`.

**`care_plan_task_completions`**: `id`, `task_id` fk, `for_date` date, `completed_by_caregiver_id` fk, `booking_id` fk, `completed_at`. `UNIQUE (task_id, for_date)` (two caregivers racing on the same tick get one row, the second call is a no-op). Unticking deletes the row and writes an event.

**`care_plan_medications`** (record only, D4): `id`, `plan_id` fk, `detail_encrypted` not null (encrypted JSON: `name`, `strength`, `dose_amount`, `dose_unit`, `route`, `reason`, `prescribed_by`, `note`), `frequency` CHECK `once_daily`/`twice_daily`/`three_times_daily`/`four_times_daily`/`weekly`/`as_needed`/`other`, `times_of_day` text[] (`"08:00"`), `start_date` date, `end_date` date null, `status` CHECK `active`/`stopped`, `stopped_at`, `created_at`, `updated_at`. A medication is **stopped, never deleted**: the history of what a client was on matters more than a tidy list.

**`care_plan_observations`** (append-only): `id`, `plan_id` fk, `booking_id` fk null, `author_caregiver_id` / `author_admin_id` (exactly one), `category` CHECK `observation`/`special_note`, `body_encrypted` text null (null only after erasure), `family_visible` bool not null default false, `created_at`. Index `(plan_id, created_at DESC)`. No edit or delete endpoint: a correction is a new note. **No database trigger** (unlike `admin_audit_log`), because erasure must blank `body_encrypted`; append-only is enforced in the service layer and tested.

**`care_plan_events`** (append-only history): `id`, `plan_id` fk, `actor_type` CHECK `caregiver`/`admin`/`system`, `actor_id` uuid, `action` text (`plan.create`, `plan.update`, `plan.status`, `template.use`, `goal.add|update|remove`, `task.add|update|remove|untick`, `med.add|update|stop`, `observation.add`), `detail` json (**field names, ids and counts only, never health text**), `created_at`. Index `(plan_id, created_at)`. This answers "who changed this plan and when" without copying health data into a second table.

### 3.4 Encryption details

- `encrypt()` / `decrypt()` from `app/core/security.py:41-46`, explicit in the service layer, stored in `*_encrypted` Text columns (the `care.py:28` / `booking.py:80-82` pattern).
- **No plaintext fallback.** `PatientVital` swallows a decrypt error and returns the raw column (`patient_vital.py:30-48`); that hides a wrong key. For plans a decrypt failure raises, the request is a 500 with a log line that names the plan id and column, never the value.
- Structured values (allergies, special instructions, a medication's detail) are one encrypted JSON string, not one column per field.
- Add to `scripts/reencrypt_health_data.py:36-42`: `care_plans.diagnosis_encrypted`, `.allergies_encrypted`, `.special_instructions_encrypted`; `care_plan_goals.title_encrypted`, `.target_encrypted`; `care_plan_tasks.title_encrypted`; `care_plan_medications.detail_encrypted`; `care_plan_observations.body_encrypted`; all `plaintext_fallback=False`. A test asserts every `*_encrypted` column on the care plan models appears in that list.
- **Assumption A2:** D6 names notes, medications and allergies. I also encrypt diagnosis, goals, task titles and special instructions, because the one-sentence rule (3.1) is simpler and a diagnosis is plainly health data. This is strictly stronger and costs nothing queryable. Template content is not encrypted (it is not about a person).
- Not covered (same as vitals): ciphertext is not bound to its row, so someone with database write access could swap two values. Out of scope; noted as a risk.

### 3.5 What each design field maps to

| Design (frame) | Stored as |
|---|---|
| Header: name, age, sex, "Lekki Phase 1" (`8.22.56`) | Not stored: name, age, sex from the existing client profile endpoint; the area is **not shown** (the profile deliberately has no address, `schemas/caregiver_client.py:1-3`). |
| "Active Care Plan" chip | `status` |
| Create form: Age, Gender, Patient Tag, Address, Emergency Contact (`8.23.57`) | **Dropped.** Already known from the booking and profile; the address must not be shown to a caregiver before and outside the booking rules; there is no "tag" concept in the code. **Assumption A3:** drop the tag (the design's `OAK123`); confirm with the designer. |
| Medical Diagnosis, Risk Level, Start Date, Goals, Activities, Any Special Instructions | `diagnosis`, `risk_level`, `start_date`, goals, tasks, `special_instructions` |
| "Plan Duration 30 Days (Review Every 2 Weeks)", "Assigned Caregiver Nurse Mary Okoro" | `duration_days`, `review_every_days`; `author` (the creator's name). |
| Care Goals list with right-aligned "Daily/Weekly/Ongoing" (`8.23.17`) | goal `title`, `target` (the grey second line), `frequency` |
| Medication form: Goal, Outcome, Name, Reason, Dosage, Usage Time, Duration, Start/End Date, Reminder Notes (`8.23.28`) | `name`, `reason`, `dose_amount`+`dose_unit`, `frequency` + `times_of_day`, `start_date`/`end_date` (Duration is a UI helper that fills `end_date`), `note`. **Medication Goal and Outcome are dropped** (a goal belongs in Care Goals; "reason" covers the purpose). The design's "Reason" field is pre-filled with the drug name, a design slip. |
| Daily tasks: day tabs, Morning (8:00 AM) / Evening (6:00 PM) groups, checkboxes (`8.23.45`) | tasks (`time_slot`, `days_of_week`) and `care_plan_task_completions` |
| Add Task modal: Task Name, Task Routine, Task Frequency | `title`, `time_slot`(+`time_of_day`), `days_of_week` (Daily / Weekdays / Pick days) |
| Notes & Observations with author name, comment box, Submit Observation | `care_plan_observations` (`category=observation`) |
| Allergies & Special Instructions with "Add Special Notes" (`8.23.57`) | `allergies`, `special_instructions` (authors edit); "Add Special Notes" writes an observation with `category=special_note`, shown under that card |
| Assigned list: risk, status "Ongoing/Completed", progress bar | `risk_level`, derived status label, `progress` (5.7) |

### 3.6 Erasure

`AccountErasureService` (`account_erasure.py`) must, in the same PR as the tables (slice 1, extended in 2 and 3): for a patient being erased, and for the `self` recipient row it already wipes (`:352-365`), find plans whose subject it is, set `status='archived'`, null `diagnosis`/`allergies`/`special_instructions` (so `diagnosis_encrypted` is nullable after erasure: **`NOT NULL` is enforced by the service on write, not by the column**, deliberately), null goal/task/medication/observation ciphertext, and keep ids, dates and the event trail. Recipients that are other people's data are left alone, as erasure already does today. A test erases a patient with a full plan and asserts no ciphertext remains.

## 4. Access rules

### 4.1 The check (one function, `care_plan_access.py`)

`resolve_plan_access(db, caregiver, booking_id) -> PlanAccess(booking, subject, level)` raises `PlanNotAvailable` (one 404 `"Care plan not found"`, no existence oracle) unless **all** hold, which is the client-profile rule (`caregiver_clients.py:83-95`) unchanged:

1. the booking exists, `caregiver_id == caregiver.id`, status in `accepted`/`en_route`/`in_progress`;
2. a caregiver whose `profile_status != "approved"` only for `in_progress`;
3. the booking's patient is active;
4. the subject resolves (3.2).

For routes keyed by plan id the access is derived the other way: the plan's subject must equal the subject of **one of the caller's live bookings** (query by `patient_id` or `care_recipient_id`); if several, use the nearest. Same 404 otherwise. Explicit vitals-style patient grants (`PatientCaregiverAccess`, `vitals_access.py:15-25`) are **not** used: the client profile does not use them either, and D5 says "same rule as the client profile". A caregiver loses access the moment the booking completes, is cancelled, or they drop out.

### 4.2 What each actor may do

| Actor | List/preview templates | View plan | Create/edit plan, goals, medications, tasks, allergies | Tick tasks | Add observation |
|---|---|---|---|---|---|
| Caregiver, approved, **nurse** skill, live booking | yes | yes (`accepted`, `en_route`, `in_progress`) | yes, in those three statuses | `in_progress` only | `in_progress` only |
| Caregiver, no nurse skill, live booking | no (403 `not_a_nurse`) | yes | no (403 `not_a_nurse`) | `in_progress` only | `in_progress` only (**A4**) |
| Suspended caregiver | by role | `in_progress` booking only | by role, `in_progress` only | `in_progress` | `in_progress` |
| Caregiver with no live booking for that client | by role | **404** | **404** | **404** | **404** |
| Patient / family (any token) | no | no endpoint exists in v1; a patient token on a caregiver route is 403 (groundwork for later: 4.4) | no | no | no |
| Admin: Clinical Reviewer / Super Admin | via admin API | `careplans.read` (audited) | `careplans.write` (audited) | no | admin may add a note (`careplans.write`) |
| Admin: Operations, Support, Finance, Verification, Analyst | no | no | no | no | no |

**A4 (observations):** D1 says other caregivers "view and tick tasks"; it does not mention observations. I allow any assigned caregiver to **add** an observation (append-only, attributed), because a visit note from the person who was there is the point of a shared plan and it edits nothing in the plan. If the owner wants nurse-only, it is one constant. Authoring is allowed before the visit starts (a nurse assessing right after accepting) but ticks and notes need `in_progress`, matching the vitals write rule.

**A5 (who counts as an author):** `CARE_PLAN_AUTHOR_SERVICE_TYPES = ("nurse",)` as one constant, per D1. `doctor` and `physiotherapist` are valid skills that might also be expected to author; widening is a one-line change plus a test.

Tick dates: only **today** in `HOME_TIMEZONE`; no ticking the past or future (**A6**). Past days are read-only history. Reading any day is allowed.

### 4.3 Headers, concurrency, privacy

- Every care plan response carries `Cache-Control: no-store` (health data, as `caregiver_clients.py:41`).
- Writes to a plan or its children take `expected_version`; a stale value is `409 version_conflict` with the current plan, so two authors cannot silently overwrite each other. Ticks and observations do not need it (they never overwrite).
- No health text in logs, events or error messages. Validation errors name the field, not the value.

### 4.4 Family view: groundwork only (out of v1)

The owner's decision: no family view in v1, but the data and the access check are built now, so it can be switched on later without migrating existing rows. **Nothing in this section is exposed by any v1 endpoint.**

**The flag.** `family_visible boolean not null default false` on `care_plan_goals`, `care_plan_tasks` and `care_plan_observations` (3.3; "notes" are observations, both categories). Default false means every item stays private until someone deliberately shares it. No v1 request body carries the flag, so v1 cannot set it by accident. The plan header (diagnosis, risk level, dates) and medications have no flag: they are not family-visible until a later decision adds one.

**The access rule** (written and tested in B1, called by nothing yet). `resolve_family_access(db, user, plan_id)` allows a person only when **all** hold:

1. a `care_relationships` row links their user to the plan's subject (`contact_user_id = user.id`, `recipient_id` = the plan's `care_recipient_id`), role `family` or `guardian`;
2. that row has `clinical_access = true`;
3. the latest `share_with_family` consent for the subject is `granted` (the `consents` table is append-only; the latest row per subject and type wins);
4. the subject is active and the plan is not erased.

It returns **only items with `family_visible = true`**, never the fields in the next list, and refuses with one 404 like the caregiver routes.

**What the code supports today** (checked):

| Question | Answer |
|---|---|
| Several relatives per person? | Yes. `uq_care_relationships_user_recipient` is unique per (user, recipient), so any number of relatives can link to one care recipient (`care.py:38-40`). |
| A permission field? | One coarse boolean, `clinical_access` (default false, `care.py:47`), set true today only for the `self` row (`channels/whatsapp/identity.py:53-54`). No per-relative scope ("goals only", "no notes"). |
| A consent field? | A `share_with_family` consent type exists (`consent.py:9`, constraint in migration `0004`); nothing reads or writes it yet. |
| Plans keyed by `patient_id` | Relationships point at `care_recipients`, not patients. WhatsApp users have a `self` recipient row; an app-only patient has none, so the rule for these plans is settled when the family view is built (**A12**). |

So several relatives is supported; a per-relative permission is not: every linked relative with `clinical_access` and consent sees the same shared items.

**Never family-visible by default (flag for clinical sign-off, with the template review in section 7).** Even if a later release adds share controls, these stay with the care team unless a clinician allows each one explicitly:

- internal observations and special notes written for the care team (behaviour, mood, concerns, family dynamics, safeguarding), unless that single item is shared;
- diagnosis, risk level, medications (name, dose, reason, prescriber) and allergies, until a separate decision;
- who wrote or ticked an item and when (staff identity and timing);
- the event history (`care_plan_events`), progress percentages, and template review status;
- anything about other people (another relative, another client).

Sharing health data with relatives needs the client's own consent, which `share_with_family` models; the compliance owner should confirm the wording.

**Delivery surface.** The W4 web companion (not built) is the intended first place a relative would see shared items, behind its magic-link and step-up authentication (`PRD-whatsapp-patient-channel.md` WA-FR-43 and WA-FR-44: clinical data only after step-up, never in chat). Nothing is designed here for it beyond the data rules above.

## 5. Behaviour decisions the design does not draw

### 5.1 Add-goal form (frame `8.23.17` shows only the success screen)

A modal like the add-task modal: **Goal** (required, max 120), **Target / measure** (optional, max 200, e.g. "1.5 L fluids a day"), **Frequency** (Daily / Weekly / Ongoing / One-time). Save calls `POST .../goals`; the success screen the design draws is kept ("New Care Goal has been added", "View Care Goal" scrolls to the goal). A goal has an overflow menu for authors: Mark achieved, Discontinue, Edit.

### 5.2 Template preview and "Use template"

- **Preview** is a read-only screen: name, summary, "30-day program, review every 14 days", the goals, the tasks grouped by slot, the special instructions, a note "Edit anything after you create the plan", and, while `clinical_review_status = pending`, a quiet "Not yet clinically reviewed" line. Button: **Use template**.
- **Use template** opens a **client picker**: the nurse's clients on live bookings that have **no active plan** (from `GET /caregiver/care-plans/candidates`, 6.1). Pick one, then the create form opens pre-filled (diagnosis empty and required, risk defaulted, start date today, goals and tasks copied and editable). Submit creates the plan (`POST .../care-plan` with `template_id`). Entering from a client's profile skips the picker.
- Templates never carry medications or allergies.

### 5.3 Create plan form

Fields: Diagnosis (required), Risk level (required, Low/Medium/High), Start date (default today), Duration (days, prefilled from the template or 30), Review every (days, optional), Goals (0 or more, inline add), Tasks/"Activities" (0 or more: title, slot, days), Special instructions (optional list). Allergies are entered on the plan's Allergies card after creation or in this form (optional). Medications are **not** in the create form (own section, own form).

### 5.4 Edit Plan (the pencil on the header; not drawn)

An edit sheet for the header fields only: Diagnosis, Risk, Start date, Duration, Review every, plus actions **Mark plan completed** and **Archive plan**. Each section (goals, medications, tasks, allergies/instructions) is edited in place through its own edit icon, visible to authors only (the response's `capabilities.can_edit_plan`). Completing a plan makes it read-only and frees the client for a new active plan. A completed or archived plan is still visible to assigned caregivers as history, labelled so.

### 5.5 Task day view

The day tabs show the current Mon-Sun week. Today is tickable if the caller can tick; other days are read-only. A task appears on a date when `days_of_week` includes it, the task is active and the date is within the plan's dates. Tasks are grouped by slot (Morning/Afternoon/Evening/Night) with the optional time in the heading as in the design. A ticked row shows who and when ("Nurse Saliu, 09:14").

### 5.6 Medications screen

A banner at the top of the section, always visible: **"This is a record of what the prescriber ordered. Oakly does not check doses or interactions and does not remind anyone to give medicines. Follow the prescriber's instructions."** (copy for clinical sign-off). Authors see Add / Edit / Stop; everyone assigned sees the list. No alerts, no "given" ticks (**A7**: administration is a visit task, "Medication administration" in the design, ticked like any task, with no per-dose record).

### 5.7 View Progress (the button on the Assigned list; not drawn)

One screen per plan: overall percent, "day N of M", next review date, a bar per recent day (completed / scheduled tasks), goal status counts (active / achieved / discontinued), last observation time. **Percent = ticked task-occurrences / scheduled task-occurrences, counted only over days that had a visit for this client** (**A8**): a nurse who visits Mon/Wed/Fri cannot tick Tuesday, so counting Tuesday would make every plan look half-done. A "visit day" is a date on which a booking for the same subject reached `in_progress` or `completed` (using `actual_start_time` in `HOME_TIMEZONE`). A task counts from the later of the plan start and the day it was added. No visit days yet: `overall_percent: null`, shown as "No visits yet".

## 6. API

All caregiver routes: `Depends(get_current_caregiver)`, mounted under `/api/v1/caregiver`, tag `caregiver-care-plans`. Unified refusal for no access: `404 {"detail": "Care plan not found"}`. Validation errors are the usual 422. `/care-plans/candidates` must be declared before `/care-plans/{plan_id}` (UUID path parameter).

### 6.1 Caregiver endpoints (22 new routes, #2-#23, plus one added Home field, #1)

| # | Method and path | Who | Purpose |
|---|---|---|---|
| 1 | `GET /caregiver/home` (existing, **add field** `can_author_care_plans: bool`) | caregiver | the nurse test for the app, computed from `caregiver_skills` (`caregiver_home.py:145-155`). Not in `CaregiverProfile` because Home is what the app already loads |
| 2 | `GET /caregiver/care-plan-templates` | nurse | list for the Templates tab |
| 3 | `GET /caregiver/care-plan-templates/{id}` | nurse | preview |
| 4 | `GET /caregiver/care-plans?status=active\|completed&q=` | caregiver | **Assigned Care Plan** tab: plans for the subjects of my live bookings; search by client name |
| 5 | `GET /caregiver/care-plans/candidates` | nurse | live-booking clients **without** an active plan, for the picker |
| 6 | `GET /caregiver/bookings/{booking_id}/care-plan` | caregiver | summary for a visit: `plan` or `null`, plus capabilities. 200 with `plan: null` means "access fine, no plan yet"; 404 means no access |
| 7 | `POST /caregiver/bookings/{booking_id}/care-plan` | nurse | create (blank or from `template_id`); 201 with the full plan |
| 8 | `GET /caregiver/care-plans/{plan_id}` | caregiver | full plan: header, goals, medications, tasks (definitions), allergies, special instructions, counts |
| 9 | `PATCH /caregiver/care-plans/{plan_id}` | nurse | header fields, allergies, special instructions, `status` to `completed`/`archived`; `expected_version` |
| 10-12 | `POST /caregiver/care-plans/{plan_id}/goals`, `PATCH /caregiver/care-plan-goals/{id}`, `DELETE /caregiver/care-plan-goals/{id}` | nurse | add; edit or change status; remove (sets `discontinued`, row kept) |
| 13-15 | `POST /caregiver/care-plans/{plan_id}/medications`, `PATCH /caregiver/care-plan-medications/{id}`, `POST /caregiver/care-plan-medications/{id}/stop` | nurse | add; edit; stop (sets `status='stopped'`, `stopped_at`) |
| 16-18 | `POST /caregiver/care-plans/{plan_id}/tasks`, `PATCH /caregiver/care-plan-tasks/{id}`, `DELETE /caregiver/care-plan-tasks/{id}` | nurse | add; edit; remove (`is_active=false`, completions kept) |
| 19 | `GET /caregiver/care-plans/{plan_id}/tasks?date=YYYY-MM-DD` | caregiver | the day view, with `done` per task |
| 20 | `PUT /caregiver/care-plan-tasks/{id}/completion` body `{"date","done"}` | caregiver, `in_progress` | tick or untick, idempotent; today only |
| 21 | `GET /caregiver/care-plans/{plan_id}/observations?before=<ts>&limit=` | caregiver | newest first, keyset paging |
| 22 | `POST /caregiver/care-plans/{plan_id}/observations` | caregiver, `in_progress` | append a note (`category` default `observation`) |
| 23 | `GET /caregiver/care-plans/{plan_id}/progress` | caregiver | View Progress (5.7) |

The handoff estimated about 15 endpoints; the write surface behind the add-goal, add-task and add-medication forms is what grew it.

**Shape sketches** (proposals; placeholders are obviously fake):

```
GET /caregiver/bookings/{booking_id}/care-plan            200
{
  "booking_id": "00000000-0000-4000-8000-000000000001",
  "subject_ready": true,
  "can_author": true,
  "plan": null          // or the summary below
}

plan summary / full plan (GET .../care-plans/{plan_id})    200
{
  "id": "…", "version": 3, "status": "active",
  "booking_id": "…",                       // the live booking that gives this caller access
  "client": {"full_name": "Placeholder Client", "age": 71, "gender": "female"},
  "diagnosis": "Hypertension, early dementia",
  "risk_level": "high",
  "start_date": "2026-10-28", "duration_days": 30, "review_every_days": 14, "review_due_on": "2026-11-11",
  "template": {"id": "…", "name": "Stroke Recovery", "version": 1} ,   // or null
  "author": {"name": "Placeholder Nurse", "kind": "caregiver"},
  "allergies": ["Placeholder allergen"],  "special_instructions": ["Placeholder instruction"],
  "goals": [{"id": "…", "title": "…", "target": "…", "frequency": "daily", "status": "active"}],
  "medications": [{"id": "…", "name": "…", "strength": "…", "dose_amount": "1", "dose_unit": "tablet", "route": "oral",
                   "frequency": "once_daily", "times_of_day": ["08:00"], "start_date": "…", "end_date": null,
                   "reason": "…", "note": "…", "status": "active"}],
  "tasks": [{"id": "…", "title": "…", "time_slot": "morning", "time_of_day": "08:00", "days_of_week": [1,2,3,4,5,6,7]}],
  "capabilities": {"can_edit_plan": true, "can_tick_tasks": true, "can_add_observation": true}
}

POST /caregiver/bookings/{booking_id}/care-plan  body
{ "template_id": "…" | null, "diagnosis": "…", "risk_level": "medium", "start_date": "2026-10-28",
  "duration_days": 30, "review_every_days": 14,
  "goals": [{"title": "…", "target": "…", "frequency": "daily"}],
  "tasks": [{"title": "…", "time_slot": "morning", "time_of_day": "08:00", "days_of_week": [1,3,5]}],
  "allergies": ["…"], "special_instructions": ["…"] }

PUT /caregiver/care-plan-tasks/{id}/completion  body  {"date": "2026-10-28", "done": true}   200
{ "task_id": "…", "date": "2026-10-28", "done": true, "done_by": "Placeholder Nurse", "done_at": "…" }

GET /caregiver/care-plans/{plan_id}/tasks?date=2026-10-28   200
{ "date": "2026-10-28", "is_today": true, "can_tick": true,
  "slots": [{"slot": "morning", "time_of_day": "08:00",
             "tasks": [{"id": "…", "title": "…", "done": true, "done_by": "…", "done_at": "…"}]}] }

GET /caregiver/care-plans/{plan_id}/progress   200
{ "overall_percent": 40, "scheduled": 42, "completed": 17, "visit_days": 6, "day": 9, "of_days": 30,
  "review_due_on": "2026-11-11",
  "by_day": [{"date": "2026-10-28", "scheduled": 5, "completed": 3}],
  "goals": {"active": 3, "achieved": 1, "discontinued": 0}, "last_observation_at": "…" }

GET /caregiver/care-plans?status=active     200   (Assigned tab; no health text beyond diagnosis)
{ "items": [{"id": "…", "booking_id": "…", "client": {"full_name": "…", "age": 72, "gender": "male"},
             "diagnosis": "Stroke", "risk_level": "high", "status": "active", "progress_percent": 40, "start_date": "…"}] }
```

**Error table** (codes are in `detail.code` next to a human `detail.message`, like the existing refund and payout errors; to be confirmed against that pattern in slice 1):

| Status | `code` | When |
|---|---|---|
| 404 | (plain `"Care plan not found"`) | no access of any kind; one answer, no oracle |
| 403 | `not_a_nurse` | an authoring route or the templates routes called by a non-nurse |
| 409 | `care_plan_exists` | create, but the client already has an active plan (body returns its id; the app opens it) |
| 409 | `client_not_identified` | an app booking for someone else, no recipient (3.2) |
| 409 | `version_conflict` | `expected_version` is stale (body returns the current plan) |
| 409 | `plan_not_editable` | the plan is `completed` or `archived` |
| 409 | `visit_not_in_progress` | tick or observation outside an in-progress visit |
| 422 | `task_date_not_today` | tick for any day but today |
| 422 | (validation) | field limits: diagnosis 1-200, titles 1-120, text 1-1000, observation 1-2000 chars, at most 30 goals, 60 tasks, 40 medications per plan |

### 6.2 Admin endpoints (Clinical Reviewer, slice 4; 9)

New permissions in `admin_permissions.py` and a data migration (the `0013` pattern): `careplans.read`, `careplans.write`, `careplans.templates`. Roles: Clinical Reviewer gets all three, Super Admin everything (already `ALL_PERMISSIONS`), nobody else.

| Method and path | Permission | Purpose |
|---|---|---|
| `GET /admin/care-plans?status=&q=&page=` | `careplans.read` | list across clients |
| `GET /admin/care-plans/{id}` | `careplans.read` | full plan; **writes an audit row** `care_plan.view` (like `booking.clinical_view`) |
| `POST /admin/care-plans` | `careplans.write` | create for a subject given as `patient_id` or `care_recipient_id`, optional `template_id` |
| `PATCH /admin/care-plans/{id}` | `careplans.write` | header, allergies, instructions, status |
| `POST/PATCH` on goals, medications, tasks, and `POST .../observations` | `careplans.write` | the same operations as 10-18 and 22, one thin admin router over the **same service functions** |
| `GET /admin/care-plan-templates` | `careplans.read` | list with review status |
| `POST /admin/care-plan-templates/{id}/review` | `careplans.templates` | sign-off: `approved`/`pending`, records reviewer and time |

Admin writes call `AuditService.record(...)` in the same transaction (`audit.py:36-60`) with before/after limited to **field names and ids** (the audit log is readable by Finance via `audit.read`, so it must not carry health text). Plan content is never put in an audit `before`/`after`. Template create/edit and the console screens are later (D3).

## 7. Seeded templates (for clinical sign-off)

Seeded by migration `0015` with `clinical_review_status='pending'`, `is_active=true`. They contain routine care tasks and goal wording only: **no medicines, no doses, no numeric thresholds** (those belong to the nurse and the prescriber, D4). The app shows "Not yet clinically reviewed" on pending templates; flipping a template to `approved` is the admin review endpoint (slice 4) or, until then, a one-line data migration after a clinician has read it. **Go-live gate: every seeded template reviewed or removed.** Everything below is a draft for a clinician to rewrite.

| Key / name | Days (review) | Default risk | Goals | Tasks (slot) | Special instructions |
|---|---|---|---|---|---|
| `stroke_recovery` Stroke Recovery | 30 (14) | high | Safe mobility and transfers; Daily range-of-motion practice; Support speech and swallowing exercises as taught by the therapist; Prevent falls | Check and record BP and pulse (morning, evening); Assisted washing and dressing (morning); Prescribed exercises with the therapist's sheet (afternoon); Safe-swallow check at meals (all meals); Skin and pressure-point check (evening) | Follow the therapist's exercise sheet; Report new weakness, slurred speech or confusion at once |
| `diabetes_management` Diabetes Management | 30 (14) | medium | Regular meals and fluids; Blood glucose checked and recorded as prescribed; Daily foot check; Gentle daily activity | Check and record blood glucose as the prescriber ordered (morning, evening); Meal support, note what was eaten (morning, afternoon, evening); Foot and skin check (evening); 30-minute walk if able (afternoon) | Record readings, do not interpret them; Report dizziness, sweating or unusual drowsiness at once |
| `post_surgery_recovery` Post-Surgery Recovery | 21 (7) | medium | Wound kept clean and dry as instructed; Pain reported and recorded; Gradual return to movement; Adequate fluids and nutrition | Wound check and record appearance (morning, evening); Assisted short walk (morning, afternoon); Hygiene and clean clothing (morning); Fluids and meal support (all meals) | Wound care exactly as the surgical team instructed; Report fever, spreading redness or discharge at once |
| `cardiac_rehabilitation` Cardiac Rehabilitation | 45 (14) | high | Progressive light activity as cleared by the care team; Resting BP and pulse recorded; Low-salt, regular meals; Report chest symptoms early | Check and record BP and pulse (morning, evening); Light activity as instructed (afternoon); Weigh and record weight (morning); Meal support (all meals) | Stop activity and call for help for chest pain, breathlessness or fainting; Follow the cardiac team's activity limits |
| `elderly_daily_living_dementia` Elderly Daily Living and Dementia Support | 30 (14) | medium | Calm, predictable daily routine; Safe home and fall prevention; Regular meals, fluids and toileting; Keep the client engaged | Morning routine: wash, dress, breakfast (morning); Hydration prompts (all day); Gentle activity or conversation (afternoon); Evening routine and a calm bedtime (evening); Note mood and confusion (evening) | Do not leave the client alone if wandering is a risk; Note any new confusion or change in behaviour |

These are descriptive of the design's sample plans (the stroke, diabetes, post-surgery and cardiac cards in frame `8.22.56`; the "Check blood sugar before each meal" and "Notify family of any falls" lines in `8.20.26`). The fifth is mine, because the sample client in the design (hypertension, early dementia) is the most common home-care case.

## 8. Screens in `apps/caregiver`

New folder `src/screens/careplan/`, stack routes next to `ClientProfile`, query keys `carePlans`, `carePlan(id)`, `carePlanTasks(id, date)`, `carePlanObservations(id)`, `carePlanProgress(id)`, `carePlanTemplates` in `src/api/keys.ts`. Health data is never written to AsyncStorage or the query persister; React Query `gcTime` short, cleared on logout, as the client profile does.

| Screen (design frame) | Calls | Slice |
|---|---|---|
| Home "CarePlan" card, nurse only, opens the hub (`8.20.26`) | `GET /caregiver/home` (`can_author_care_plans`) | M1 |
| Hub: Assigned / Templates tabs, search, "Create New Care Plan" (`8.22.56`) | #4, #2, #5 | M1 |
| Template preview + client picker (**new**) | #3, #5 | M1 |
| Create plan form + success (`8.23.57`) | #7 | M1 |
| Care Plan Details accordions: client profile (existing endpoint), goals, allergies/special instructions (`8.23.17`, `8.23.57`) | #8, `GET /bookings/{id}/client` | M1 |
| Add goal modal (**new**) + success | #10-12 | M1 |
| Edit Plan sheet (**new**) | #9 | M1 |
| Client profile: "Care plan" summary row and Create/Open button | #6 | M1 |
| Daily Care Task: day tabs, ticks, Add Task modal + success (`8.23.45`) | #19, #20, #16-18 | M2 |
| Notes & Observations, special notes (`8.23.45`, `8.23.57`) | #21, #22 | M2 |
| View Progress (**new**) | #23 | M2 |
| Medication Schedules list, Add Medication form + success, "not clinical advice" banner (`8.23.17`, `8.23.28`) | #13-15 | M3 |

Fixed type tokens only (`theme.ts`, no raw `fontSize`), the existing success-screen component, and the same `GateMessage`-style fallback for "not available during an active visit".

## 9. Failure and empty states

| State | Behaviour |
|---|---|
| No plan yet | Details: "No care plan yet". Nurse sees "Create care plan" and "Use a template"; others see "Ask the nurse on this visit to create one". |
| Client not identified (3.2) | "We can't tell who this visit is for, so a care plan isn't available. Contact support." No create button. |
| 404 on any plan call | "This care plan isn't available. You can see a client's plan while their visit is accepted, on the way or in progress." (the loss of access is normal at clock-out) |
| 403 `not_a_nurse` | actions are hidden by `capabilities`; if one is reached anyway, "Only nurses can change a care plan". |
| 409 `care_plan_exists` | open the existing plan. |
| 409 `version_conflict` | "Someone just changed this plan", reload, keep the user's draft text in the form. |
| `visit_not_in_progress` | tick boxes disabled with "Available during an active visit". |
| No tasks / no goals / no medications / no observations | per-section empty text; authors get the add button. |
| Offline | reads show nothing (no persistence of health data); writes fail with a retry, never queued (ticks could be double-applied or applied to a finished visit). |
| Completed or archived plan | read-only banner. |
| Erased client | 404 (the patient is inactive, same as the profile). |

## 10. Audit, privacy and risks

**Audit.** Caregiver writes are attributed by columns (`completed_by_caregiver_id`, `author_caregiver_id`) and by `care_plan_events` (no health text). Admin reads and writes also go to `admin_audit_log`. Caregiver **reads** are not logged (as the client profile and vitals are not); revisit if the compliance owner asks. No health text in logs or metrics. Care plan reads set `no-store`.

**Privacy.** No address, phone, email, or booking condition on any care plan response. Family cannot see anything. Erasure wipes content (3.6). Retention beyond erasure is not defined: **Assumption A9:** plans are kept as long as the client exists; the owner should decide a retention period with the compliance review.

**Risks**

| Risk | Mitigation |
|---|---|
| Wrong client's plan shown (booker vs relative) | The subject rule copies the profile's (3.2); tests for each branch; an Opus review of this exact function |
| A decrypt failure becoming a silent blank | No plaintext fallback (3.4); 500 with a column-level log |
| Stale overwrites between two authors | `expected_version`, `409 version_conflict` |
| Clinical liability from seeded content and medications | Templates carry no medicines or numbers; banner on medications; go-live gate on template sign-off (7) |
| Allergies exist in two places: the patient record (`patient.py:39`, shown in the profile's medical history) and the plan | Both are shown, each labelled by source; the plan's list is for the care team, the patient record is the client's own. Do **not** merge them in v1; flag to the owner (section 12) |
| A caregiver who completes a visit cannot tick the last task or add a closing note after clock-out | Intended (same as the profile); the app should prompt for notes **before** clock-out (a small M2 item) |
| A nurse with several clients and one plan per client cannot choose between two plans | One active plan per client (partial unique index); history stays in completed/archived plans |
| Scope creep into clinical advice (dose alerts, reminders) | Non-goals (section 1) and D4 |

## 11. Contradictions between the handoff and the code

1. **"A plan belongs to the patient/care recipient"** (`HANDOFF-master.md` 2a.5) assumes one key. In code the booking's `patient_id` is the **booker** when the visit is for someone else, `care_recipient_id` exists only on WhatsApp bookings, and an app booking for someone else has no identifiable client (`caregiver_clients.py:97-106`, `booking.py:76-79`). The design keys by *subject* (3.2) and refuses plans where there is none (A1).
2. **"Caregiver skill `nurse`"** cannot be used by the app as written: skills are not in any caregiver-facing response (`schemas/caregiver.py:58-80`, `caregiver_app.py:46-57`), and the only caregiver-facing field, `specialization`, is self-declared text. A server-computed `can_author_care_plans` is added to Home.
3. **"A Clinical Reviewer creates/edits plans"** has no home today: it is an admin role with no care plan permission and the console has no care plan screens (`admin_permissions.py:91-94`). v1 delivers an **admin API** (slice 4); the console screens stay out of scope, so a Clinical Reviewer cannot author from a browser until a later console PR.
4. The **design's create form** asks for address, age, gender, emergency contact and a patient tag. The code deliberately keeps the address out of the caregiver's view (`schemas/caregiver_client.py:1-3`) and has no tag concept; the design's form is cut down (3.5).
5. The handoff says "about 15 endpoints"; the design's screens need 22 new caregiver routes (plus one Home field) and 9 admin routes. The writes behind the add-goal, add-task and add-medication forms account for most of the difference.
6. Minor: the handoff and checklist say the vitals/profile access is "the same rule". It is not quite: vitals also accept an explicit patient grant (`vitals_access.py:25`), the profile does not. Care plans follow the **profile** (D5).

## 12. Assumptions for the owner to confirm

| # | Assumption | Cost if wrong |
|---|---|---|
| A1 | App bookings for someone else (no recipient) get no care plan in v1 | Small today (WhatsApp is the live channel); later change touches bookings and erasure |
| A2 | Diagnosis, goals, task titles and special instructions are encrypted as well | None |
| A3 | The design's "Patient tag" is dropped | Ask the designer; a column later |
| A4 | Any assigned caregiver (not only a nurse) may add observations | One constant |
| A5 | Only `nurse` authors (not `doctor` or `physiotherapist`) | One constant |
| A6 | Ticking only for today, during an in-progress visit | A looser rule is a one-line change |
| A7 | No per-dose medication record; administration is a ticked task | A later table |
| A8 | Progress counts only days with a visit | The number will look low if counted over all days |
| A9 | Plans are kept while the client exists; retention period to be decided | Compliance |
| A10 | Seeded templates go live as `pending` review, shown with a "not yet clinically reviewed" line | Alternative: hide until approved (the Templates tab would be empty at launch) |
| A12 | Family access reuses `clinical_access` plus the `share_with_family` consent; a per-relative scope and the rule for app-only (`patient_id`) subjects are decided when the family view is built | The access function is the one place to change |
| A11 | The plan's allergies and the patient record's allergies stay separate and both are shown | Merging needs a data rule |

## 13. Test strategy

pytest with the existing factories; one file per slice plus a migration test (the `test_refunds_migration.py` pattern).

- **Access (most important, parametrised):** for every caregiver route, each booking status (`pending`, `accepted`, `en_route`, `in_progress`, `completed`, `cancelled`, `declined`), the other caregiver's booking, a missing booking, an erased patient, a suspended caregiver (`accepted`/`en_route` refused, `in_progress` allowed), a patient token (403), no token (401), drop-out loses access; **one identical 404** body for all of them.
- **Subject:** `myself` (patient subject), WhatsApp for-someone-else (recipient subject), app booking for someone else (`subject_ready: false`, create `409 client_not_identified`), `Myself` casing.
- **Roles:** nurse vs non-nurse on every authoring route (403 `not_a_nurse`), including a caregiver whose **self-declared** `specialization` says "nurse" but has no skill row; Clinical Reviewer allowed, Operations/Support/Finance denied (`test_admin_rbac.py` pattern); a Clinical Reviewer read writes exactly one audit row.
- **Encryption:** raw column values are not the plaintext; round trip; a wrong key raises; rotation test (`reencrypt_health_data` covers every `*_encrypted` column, asserted by a model-introspection test); no response carries ciphertext.
- **Plan rules:** one active plan per subject including a concurrent create; copy-not-reference from a template; completed plan read-only; `version_conflict`; field limits; soft-removed items disappear from reads but keep completions.
- **Tasks:** day matching by `days_of_week`, tick idempotent, two callers racing, untick writes an event, today-only in `Africa/Lagos` around midnight, not ticked outside `in_progress`.
- **Progress:** visit-day denominator, a task added mid-plan, no visit yet.
- **Family groundwork (B1/B2):** `family_visible` defaults false and no v1 request body can set it; `resolve_family_access` allows only a linked family/guardian with `clinical_access` and a granted `share_with_family` consent, refuses a withdrawn consent, an unlinked user and another client's relative, supports two relatives of one client, and returns only flagged items; no v1 route calls it.
- **Erasure:** a full plan, then erasure, nothing readable remains.
- **Leak test:** the response keys are an exact closed list (as the profile test does with `SECRET_BITS`): no address, phone, email, booking condition.
- **OpenAPI:** the monorepo `api:sync` drift check passes; mobile tests (Vitest/RTL, existing setup) cover each screen's empty, loading, error, 404 and capability-hidden states.
- **FE guide:** a capture script per slice (the `test_capture_client_profile.py` pattern, skipped unless an env flag is set) feeds `docs/fe-integration-guide-caregiver.md`; unverified parts labelled inline. No docker or full pytest is part of this design PR.

## 14. Slice plan

Owner feedback (2026-10-21): slices that were too big took too long. Four backend slices, three mobile slices, each reviewable on its own. **Opus** reviews every backend slice **once** (read-only, about 20 lines) with at most **one** re-review, because all of it is patient health data or access control; Sonnet implements and does routine reviews; minor findings go to the SOP's follow-ups, not new rounds. None touches money.

| Slice | Scope | Migration | Endpoints | Sensitive | Size | Opus |
|---|---|---|---|---|---|---|
| **B1 Foundation** (`feat/care-plans-core`) | Subject and access resolver; nurse test and Home field; `care_plan_templates` + 5 seeds; `care_plans`, `care_plan_goals`, `care_plan_tasks` (definitions, because create and "Use template" copy them), `care_plan_events`; templates list/preview, assigned list, candidates, visit summary, create (with goals and tasks), get, patch (incl. allergies, special instructions, complete/archive), goals add/edit/remove; `family_visible` (default false) on goals and tasks and the tested `resolve_family_access` check over `CareRelationship` and consent (4.4), not exposed; erasure hook; reencrypt list; SOP, FE guide section from captures, checklist | `0015_care_plans_core` | 1 (Home field), 2, 3, 4, 5, 6, 7, 8, 9, 10-12 | patient health data, access control | M: 11 routes plus the Home field, about 900 lines of code, about 1,100 lines of tests | yes |
| **B2 Ticks, notes, progress** (`feat/care-plans-tasks`) | `care_plan_task_completions`, `care_plan_observations`; task add/edit/remove, day view, tick, observations (with `family_visible`, default false), progress; erasure and reencrypt extended | `0016_care_plan_ticks_and_notes` | 16-23 | health data | M: 8 routes, about 700 + 900 tests | yes |
| **B3 Medications** (`feat/care-plans-medications`) | `care_plan_medications`; add, edit, stop; the "not clinical advice" copy lives in the app and in the OpenAPI description; erasure and reencrypt extended | `0017_care_plan_medications` | 13-15 | health data (medicines) | S: 3 routes, about 300 + 450 tests | yes (short) |
| **B4 Admin API** (`feat/care-plans-admin`) | `careplans.read/write/templates`, Clinical Reviewer grants, admin list/detail/create/edit/section writes/note, template review; audit rows; admin FE guide section | `0018_care_plan_admin_permissions` (data only) | 9 admin routes | auth + health data | S-M: about 500 + 600 tests (mostly reuses B1-B3 services) | yes |
| **M1 Hub, create, details, goals** (monorepo `feat/caregiver-care-plans-hub`) | Home card, hub, preview, picker, create, details accordions, add goal, edit plan, client-profile row | none | calls B1 | screens only | M | no (Sonnet review) |
| **M2 Ticks, notes, progress** (`feat/caregiver-care-plans-tasks`) | day tabs and ticks, add task, observations, view progress, "add notes before clock-out" prompt | none | calls B2 | screens only | M | no |
| **M3 Medications** (`feat/caregiver-care-plans-meds`) | list, add form, banner | none | calls B3 | screens only | S | no |

**Order and parallelism.** The backend lane is sequential because each slice adds the next Alembic revision and edits the same service and router files: **B1, B2, B3, B4**. The mobile lane is sequential too and runs one step behind: **M1 once the B1 contract is fixed** (this document, then B1's captured FE guide), M2 after B2, M3 after B3. So at most **two sessions at a time** (one backend, one mobile), inside the 2-3 agent cap. A mobile slice can start on the backend branch by pointing `OAKLY_BACKEND_DIR` at it and running `pnpm api:sync`, but it is **merged after** its backend slice (the monorepo CI checks the committed OpenAPI file, not the live backend). B4 has no mobile counterpart and can run beside M2 or M3. Dependency summary: B1 unblocks everything; B2 needs B1 tables (goals/plans); B3 needs B1; B2 and B3 do not depend on each other and could swap order, but share the erasure and reencrypt edits, so keep them sequential to avoid merge conflicts.

**Per slice, every time** (the project rules): SOP in `docs/sop/`, tick the checklist items, FE guide section from live captures (honestly labelled gaps), local CI parity (`poetry check --lock`, ruff, `ci_smoke`, both `render_fe_guide.py --check`, full pytest; monorepo: frozen install, api drift, typecheck, lint, tests), branch names with no AI wording, clean commits, never pushed or merged by the sub-session.

**Before anyone can use it for real:** a clinician reviews the seeded templates and the medication banner copy; at least one nurse skill is assigned to a test caregiver; a device pass (the whole caregiver app still has none).

## 15. Follow-ups after v1

Family read-only view through W4 (the flag and access check already exist, 4.4; it needs the companion, share controls and the clinician's list); admin console screens for plans and templates; template create/edit; medication reminders and a per-dose record (needs clinical sign-off); a care plan summary in the visit-complete flow; merging allergy sources; per-recipient vitals (shares the subject problem); creating a `CareRecipient` for app bookings; plan change push to the other caregivers; export or print for the family; a retention policy.
