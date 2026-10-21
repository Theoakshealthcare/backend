# SOP: Finance approves and cancels payouts

| | |
|---|---|
| **Date** | 2026-10-19 |
| **Branch** | `feat/finance-approves-payouts` (PR: see the checklist) |
| **Migration** | none (see "How") |
| **Spec** | Owner decision, 2026-10-19: "Finance can approve and cancel payout batches/payouts; resolve, retry, force and unfreeze stay Super Admin only." |

## What shipped
Three admin payout routes now need `payouts.execute` instead of `ledger.adjust`, so the Finance role can use them:

| Route | Permission now | Finance | Super Admin |
|---|---|---|---|
| `POST /admin/payouts/batches/{id}/approve` | `payouts.execute` | yes | yes |
| `POST /admin/payouts/batches/{id}/cancel` | `payouts.execute` | yes | yes |
| `POST /admin/payouts/{id}/cancel` | `payouts.execute` | yes | yes |
| `POST /admin/payouts/{id}/retry` | `ledger.adjust` | `403` | yes |
| `POST /admin/payouts/{id}/resolve` (incl. `force`) | `ledger.adjust` | `403` | yes |
| `POST /admin/payouts/caregivers/{id}/unfreeze` (incl. `write_off_shortfall`) | `ledger.adjust` | `403` | yes |
| reads (`GET /admin/payouts...`) | `payments.read` | yes | yes |

## Why
Until now every payout action needed `ledger.adjust`, which only the Super Admin holds. The owner wants Finance to run the weekly approve/cancel step while the exceptional, dispute-handling actions stay with the Super Admin. `payouts.execute` already existed in the permission catalogue, unused.

## How
- `app/api/v1/endpoints/admin_payouts.py`: swapped the `require_permission` argument on the three routes. No service change: each action still takes a required reason, runs under the same locks and writes the same audit row, with `admin_user_id` the acting admin (a Finance admin's id is also stored as `approved_by` / `cancelled_by`).
- **No migration.** `payouts.execute` has been in Finance's role since `0003_admin_identity` and in `ROLE_PERMISSIONS` (`app/core/admin_permissions.py`); `test_admin_migration.py` already asserts the seeded rows equal that table. A migration that re-granted it would be a no-op, so none was added. Super Admin gets every permission in `ALL_PERMISSIONS`.
- Rejected: giving Finance `ledger.adjust` (would also open retry/resolve/unfreeze and earnings void) or a new permission (no need).

## What's involved
- `app/api/v1/endpoints/admin_payouts.py` (module docstring and three dependencies).
- `app/tests/test_admin_rbac.py`: the route-to-permission table now lists `payouts.execute` for the three routes; its matrix runs every route against every role.
- `app/tests/test_payouts.py`: `test_finance_approves_and_cancels_but_cannot_retry_resolve_or_unfreeze`, `test_finance_can_cancel_a_batch`; the older API tests now use a role without the permission for the `403` case.
- `scripts/capture_admin_flow.py` and `docs/fe-captures/admin/` (payouts section re-captured live, 183 to 302), `docs/fe-integration-guide-admin.md` (permissions matrix, section 12.11 tables, verification table).

## Verification
- Targeted tests (payouts, RBAC matrix, admin migration): 143 passed; full suite and CI steps: see the PR.
- Live capture on the compose stack: Finance approve (`204`) and cancel (`197`, `280`) are `200`; Finance retry (`211`), resolve (`237`) and unfreeze (`253`) are `403` `Access denied. Missing permission: ledger.adjust`; Operations on approve/cancel is `403` `Access denied. Missing permission: payouts.execute` (`194`, `200`, `277`).

## Operate / roll back
Revert the commit; no schema change. After deploy, a Finance admin's existing token works at once (permissions are read from the database per request).

## Follow-ups
- Admin console: show Approve/Cancel on `payouts.execute` and Retry/Resolve/Unfreeze on `ledger.adjust` (separate PR in the monorepo).
- ~~Finance cannot see its own trail~~ Done the same day: Finance reads the audit log, see [the finance-audit-read SOP](2026-10-19-finance-audit-read.md).
