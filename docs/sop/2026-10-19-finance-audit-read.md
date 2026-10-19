# SOP: Finance can read the audit log

| | |
|---|---|
| **Date** | 2026-10-19 |
| **Branch** | `feat/finance-audit-read` (PR: see the checklist) |
| **Migration** | `0013_finance_audit_read` (down_revision `0012_caregiver_payouts`) |
| **Spec** | Owner decision, 2026-10-19: "yes, give Finance read access" to the audit log (follows [Finance approves payouts](2026-10-19-finance-approves-payouts.md)) |

## What shipped
The Finance role holds `audit.read`, so `GET /admin/audit` answers `200` for Finance instead of `403`. Nothing else changes: admin management (`admins.manage`) stays Super Admin only, and no other role gained `audit.read`.

## Why
Finance now approves and cancels payouts but could not see the audit trail of what it (or anyone) did.

## How
- `app/core/admin_permissions.py`: `audit.read` added to `FINANCE`.
- `app/alembic/versions/0013_finance_audit_read.py`: inserts the `(finance, audit.read)` row (`ON CONFLICT DO NOTHING`); downgrade deletes only that row. Roles are seeded by migration, not at startup, so the migration is what makes it real.
- Tests: the RBAC matrix reads `ROLE_PERMISSIONS`, so `GET /admin/audit` is now expected `200` for Finance and `403` for every other non-Super role; `test_finance_reads_the_audit_log_but_cannot_manage_admins`; `test_finance_audit_migration.py` (up, down, round trip); `test_admin_migration.py` compares `0003`'s own seed (Finance without `audit.read`); `test_admin_users.py` uses Operations for the 403.

## What an auditor sees (checked before granting)
`GET /admin/audit` is read-only (no write verbs; `POST` is `405`) and returns, for every admin action: acting admin id and **email**, action, entity type and id, `before`/`after` JSON, `reason`, **ip** and **user agent**, time.
- `before`/`after` are written through `AuditService` with keys containing `password`, `token`, `secret`, `pin`, `authorization` or `api_key` redacted.
- Contents seen in the code: status snapshots and ids (booking, caregiver, payout), amounts in kobo, admin-written reasons, role lists and invite emails (`admin.invite`), login events with ip/user agent, `caregiver.view` (no payload), `booking.clinical_view` (only the *names* of the fields revealed, never the clinical text), `caregiver.background_check_decision` evidence (notes, provider reference, document id; Finance already sees the same on the caregiver detail via `caregivers.read`), `payout.resolve` with what Paystack said, and `booking.confirm_address` with the **latitude and longitude** the admin entered (geocoded home coordinates of the visit address: about as identifying as the address, and Finance does not hold `bookings.address`).
- Not in the log: identity document numbers, bank account numbers, clinical text, MFA secrets, invite tokens.
- Residual exposure, accepted: Finance can see other admins' emails, IPs, user agents and free-text reasons (a booking cancel or assign reason can carry address or clinical detail), who was invited with which roles, and confirmed-address coordinates. **Owner sign-off needed on the coordinates**; the alternative is to drop them from the `booking.confirm_address` audit row (a small follow-up change) and keep the viewer as is.

## Verification
Targeted tests (RBAC matrix, migration, admin users); full suite and CI steps: see the PR. Not captured live (the shape is unchanged); labelled in the admin FE guide.

## Operate / roll back
Deploy runs the migration. Roll back with `alembic downgrade 0012_caregiver_payouts` (removes only Finance's row) and revert the commit. A Finance token's permissions are read per request, so the change applies at once.

## Follow-ups
- Admin console audit screen is not built (checklist A5); once it is, gate it on `audit.read` (the nav item already is).
