# SOP: Caregiver approval API and admin audit log (WP-E5)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `foundations/caregiver-approval` (base `main`) |
| **Migration** | `2e543a04ee5d` (down_revision `6fb30639d5f4`) |
| **Status** | Verified locally; not pushed. Needs the Opus permission-path review before merge. |

## What shipped
Admins can now review caregiver applications through `/api/v1/admin/caregivers`, and nobody becomes bookable without an admin decision. A **cleared background check is mandatory** before approval. Every admin action writes an `admin_audit_log` row.

| Endpoint | Purpose |
|---|---|
| `GET /` | Queue. Filter `profile_status` (default `under_review`), `limit` (1-100), `offset`. Oldest submission first. |
| `GET /{id}` | Full detail: profile, consents, documents (with the existing download URL), identity results, background check, skills, audit history. The view itself is audited. |
| `POST /documents/{doc_id}/decision` | `verified` (optional `expires_on`) or `rejected` (reason required). |
| `POST /{id}/identity-decision` | `verified` / `failed`, notes required. |
| `POST /{id}/background-check-decision` | `cleared` / `failed`, notes required, optional `provider_reference` and `document_id`. |
| `PUT /{id}/skills` | Replaces the whole skill list. |
| `POST /{id}/approve` | 409 unless every precondition holds (below). |
| `POST /{id}/reject`, `request-changes`, `suspend`, `reinstate` | State changes with reasons; the first three plus approve email the caregiver. |

Also: `caregiver_skills` table (used by E4 matching), the Nigerian states (36 plus FCT) in `GET /caregiver/form-options`, and `GET /caregiver/verification-status` now returns `rejection_reason` and `changes_requested`.

## Why
Approval was only a status string, and the consent endpoint could move any caregiver (including a suspended one) back to `under_review` and reset the background check. There was no audit trail, no skill model for matching, and no mandatory background check (AD-FR-7..14, 56, 85).

## How
| Decision | Choice | Why |
|---|---|---|
| Approve preconditions | At least one `license` document verified and none pending; identity `verified`; background check `cleared`; at least one skill; all three consents; status `under_review`. | From the plan. All unmet items are returned together. |
| 409 shape | `{"detail": {"message": "...", "unmet": [{"code": "...", "message": "..."}]}}`. Codes: `INVALID_STATE`, `LICENSE_NOT_VERIFIED`, `LICENSE_PENDING`, `IDENTITY_NOT_VERIFIED`, `BACKGROUND_CHECK_NOT_CLEARED`, `NO_SKILLS`, `CONSENTS_NOT_ACCEPTED`. | Keeps the `{"detail": ...}` contract; the console can show every missing item at once. Other 409s use a plain string. |
| State machine | approve: `under_review`. reject: `under_review` or `changes_requested`. request-changes: `under_review`. suspend: `approved`. reinstate: `suspended`. | Anything else is 409, so a decision can't be silently overwritten. |
| Concurrency | Each action does `SELECT ... FOR UPDATE` on the caregiver row (`with_for_update().populate_existing()`), then checks, changes, audits and commits once. Document decisions lock the owning caregiver. | Approve and reject racing give exactly one winner (tested). |
| Audit | `AuditService.record()` adds the row to the caller's session without committing, so the audit row and the change commit together. Sensitive keys (`password`, `token`, `secret`, `pin`, ...) are redacted. IP is the direct peer, not `X-Forwarded-For`. | A forged proxy header must not forge the log. |
| Append-only | ORM listeners refuse update/delete, and the migration adds a Postgres trigger that raises on UPDATE/DELETE for any client. | The PRD asks for DB-level protection; the role grant comes with the admin DB role later. |
| Consent endpoint | Only moves `incomplete`/`changes_requested` to `under_review`, and only moves a `pending` background check to `in_progress`. | The old code reset approved/suspended/rejected caregivers. This is how a caregiver resubmits after "request changes". |
| Accepting bookings | A caregiver must be `approved` to accept (`BookingActionForbidden`, 403). | A caregiver suspended after an offer was sent could still accept it. Matching already filters on `approved`. |
| Suspend and bookings | Upcoming `accepted` bookings are returned as `affected_bookings`, written to the audit row and logged as a warning. No booking column was added. | There is no ops queue yet, and E2 owns the bookings model. |
| Emails | `send_caregiver_decision_email` runs as a background task after commit; a failure is logged and never undoes the decision. Template autoescapes admin text. | A mail outage must not block a decision. |
| Skills uniqueness | Unique index on `(caregiver_id, service_type, coalesce(sub_specialty, ''))`. | A plain unique constraint treats NULLs as distinct. |

Rejected alternatives: a `NULLS NOT DISTINCT` constraint (needs Postgres 15+); a bookings flag column (collides with E2); trusting `X-Forwarded-For`.

## What's involved
- Models: `app/db/models/audit.py` (new), `app/db/models/caregiver.py` (new columns, `CaregiverSkill`), `app/db/models/user.py` (`foreign_keys` on the `caregiver` relationship, now needed because `caregivers` has several FKs to `users`).
- Services: `app/services/audit.py`, `app/services/caregiver_approval.py` (new); `app/services/caregiver.py` (consent rule, verification status, `NIGERIAN_STATES`); `app/services/booking.py` (accept guard, 6 lines); `app/services/email.py` plus `app/templates/emails/caregiver_decision.html`.
- API: `app/api/v1/endpoints/admin_caregivers.py`, `app/schemas/admin_caregiver.py` (extra fields forbidden), registered in `app/api/v1/api.py`.
- Migration: `app/alembic/versions/2e543a04ee5d_caregiver_approval.py`.
- Tests: `app/tests/test_caregiver_approval.py`, `app/tests/test_caregiver_approval_migration.py`.

## Verification
- Tests first: the new suite failed (no routes, no tables) before the code. Baseline on the unchanged code was 202 passed.
- Covered: 401 (no token, refresh token) and 403 (patient, caregiver) on all 11 routes; each approve precondition missing gives a 409 naming it, and all missing at once lists all five; one audit row per action with before/after; caregiver cannot set any new field through step1, step2, consent or upload; suspended caregivers are excluded from `find_nearby_caregivers` and cannot accept an earlier offer; approve vs reject race.
- Migration, automated: `test_caregiver_approval_migration.py` builds a scratch database, seeds rows, runs `downgrade()` then `upgrade()` twice, checks the rows survive, `compare_metadata` against the models is empty, and the trigger blocks UPDATE and DELETE.
- Migration, manual (Postgres 16, schema built on `main` by `ENVIRONMENT=development python -m scripts.init_local_db`, one caregiver and one document seeded):

```
alembic upgrade head    Running upgrade 6fb30639d5f4 -> 2e543a04ee5d
alembic downgrade -1    Running downgrade 2e543a04ee5d -> 6fb30639d5f4
alembic upgrade head    Running upgrade 6fb30639d5f4 -> 2e543a04ee5d
alembic current         2e543a04ee5d (head)
seeded caregiver afterwards: approved | in_progress | approved_by NULL
```

## Operate / roll back
- Deploy: `alembic upgrade head`. No data conversion.
- Roll back: `alembic downgrade -1` drops the two tables and the new columns (approval history and skills are lost). Existing `profile_status` values are untouched; a `suspended` row is treated by old code as not approved, so it stays unmatched.
- Try it: `curl -H "Authorization: Bearer $ADMIN" "$API/api/v1/admin/caregivers"`.

## Follow-ups
- Mobile must call `PUT /caregiver/consent` again to resubmit after `changes_requested`; confirm the app does, or add an explicit resubmit endpoint.
- Suspended bookings need a real ops queue (a flag column once E2 merges).
- Reinstate does not re-check preconditions (for example an expired licence); `expires_on` is stored but not enforced yet.
- Console gaps: list filters by city and specialization, masked ID numbers, a searchable audit viewer, roles and permissions.
- The audit list endpoint (`GET /`) is not itself audited; only the detail view is.
- Push notification on approval (AD-FR-11) is not sent; email only.
