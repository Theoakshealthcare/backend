# SOP: caregiver earnings (A3 slice 1)

| | |
|---|---|
| **Date** | 2026-10-07 |
| **Branch** | `feat/a3-earnings`, on `origin/main` at `9575296` |
| **Migration** | `0011_caregiver_earnings` (down_revision `0010_dropout_fee_tiers`) |
| **Spec** | `docs/plans/2026-10-06-admin-console.md` A3, PRD AD-FR-38; owner decisions 2026-10-07 |
| **Status** | Verified locally: new tests, full suite, CI checks, live capture on the compose stack. Not pushed. |

## What shipped
- Completing a visit credits the caregiver **one `earning`**: 80% of the booking's **service fee** (not `total_amount_kobo`, not the platform fee), integer kobo, rounded half up. It is `pending_clearance` with `available_at = completed_at + 24 hours`. The wallet balance does not move.
- A family cancellation in the **late tier**, `late_cancel_compensable` true, with a fee kept, credits a `late_cancel_compensation` of `caregiver_late_fee_share_percent` (80%) of the fee kept, same hold.
- ARQ `earnings_release_job` (every five minutes) moves due pending rows to `successful` and adds them to `caregiver_wallets.balance`, which is now strictly the **available** balance.
- `GET /caregiver/wallet` adds `available_balance`, `pending_balance`, `held_balance`, `total_pending`, `next_available_at`; transactions show `status`, `available_at`, `released_at`, `booking_id`.
- Admin `GET /admin/earnings`, `POST .../hold`, `.../release-hold`, `.../void` (reason required, audited). The A2 no-show flag holds the booking's pending earning.
- Settings in `booking_policies`, editable through the audited `GET`/`PATCH /admin/booking-policy` with bounds: `caregiver_visit_share_percent` 80 (0 to 100), `earning_hold_hours` 24 (0 to 168), and seeded, **read by slice 2 (payouts)**: `payout_cadence` weekly (`weekly|biweekly|monthly`), `payout_min_naira` 5000, `finance_refund_limit_naira` 50000 (0 to 100,000,000).

## Why
Caregivers were never credited: completing a visit moved no money, so `total_earned` was always 0 (AD-FR-38). The owner wanted the share and a dispute window to be admin-editable, and a caregiver never to be promised one thing and paid another.

## How
| Decision | Choice | Rejected alternative |
|---|---|---|
| Share base | `bookings.service_fee_kobo`. The platform fee is Oakly's, the total includes it. | percent of total |
| Terms | Copied onto the booking at creation (`bookings.earnings_terms` JSON: visit share, hold hours, late-fee share), like `cancel_policy`. A later policy edit never changes it. Old bookings or a damaged copy fall back to the live policy. | read live at completion |
| Idempotency | Two layers: the credit function looks for the row first (it runs under the booking `FOR UPDATE` lock the complete action already takes), and **unique `(booking_id, type)`** refuses a second row whatever the caller. Withdrawals have no booking (NULLs are distinct). | lock only, or constraint only |
| Wallet balance | `balance` is available money only; pending lives in the transaction rows and is summed on read. | a second `pending` column on the wallet |
| Lock order | booking, then transaction row, then wallet. The credit paths take no wallet lock because nothing moves. | |
| Release | `FOR UPDATE SKIP LOCKED` batch of due `pending_clearance` rows, status to `successful`, one atomic wallet increment per caregiver in a fixed order, commit per batch. Held and voided rows are never selected. | per-row loop |
| Hold, void | `held` is reversible (`release-hold`, the original `available_at` still applies); `voided` is final. A released row cannot be held or voided: taking money back is a ledger adjustment (AD-FR-42, later). | |
| Permissions | Read `payments.read` (Finance, Super Admin); hold and release-hold `ledger.adjust_request` (Finance, Super Admin); void `ledger.adjust` (Super Admin only). | |
| No-show | Flag on an accepted booking holds any pending earning; if the booking is completed later, the earning is credited **already held**. | |
| Wire units | Wallet endpoints keep **kobo** (the caregiver app formats with `formatKobo`; `/caregiver/home` is the only naira endpoint). The new fields are kobo too, so one object never mixes units. Policy `_naira` fields are naira. | naira on `/wallet` (breaks `balance` for installed apps) |

## What's involved
- `app/services/earnings.py` (credit, balances, `release_due`, hold, release-hold, void, no-show hold); `app/api/v1/endpoints/admin_earnings.py`, `app/schemas/admin_earning.py`.
- Hooks: `BookingService` complete action (`app/services/booking.py`), admin `correct-status` to completed and the no-show flag (`app/services/admin_bookings.py`), `cancel_by_booker` (`app/services/booking_cancellation.py`).
- `app/db/models/caregiver.py` (transaction columns, `uq_caregiver_transactions_booking_type`, type and status checks, release index), `booking_policy.py`, `booking.py`; `app/services/booking_policy.py` and schema (validation); `app/api/v1/endpoints/caregiver.py` (wallet, transactions, payslip counts released earnings; `/wallet/withdraw` was unchanged and available-only by construction in this slice; **removed in slice 2**, see [caregiver payouts](2026-10-12-caregiver-payouts.md)); `app/services/account_erasure.py` (refused while an earning is pending or held); `app/worker.py` (cron).
- Migration `0011`: adds the columns and checks, seeds the defaults through the existing row, unique constraint, partial index. Downgrade deletes earning and compensation rows (a balance already released stays in the wallet) and drops everything it added.

## Verification
- `app/tests/test_caregiver_earnings.py` (72 tests) and `test_earnings_migration.py`, plus updated policy, worker and dropout-migration tests.
- Mutation checks, all caught: feature off (39 fail); booking lock removed (both concurrency tests fail: a duplicate insert hits the constraint and the request errors); lock removed and the unique constraint dropped from the schema (the constraint test and both concurrency tests fail); existing-row check removed (the constraint catches it, one idempotency test fails).
- Live capture on the compose stack: caregiver guide captures 154 to 168 and admin 155 to 182. The release job is the real function run in the worker container; the 24-hour wait is a labelled database edit.

## Operate / roll back
- The worker must run (`docker compose up -d` includes it). The job is safe to run by hand: `from app.services.earnings import release_due; release_due(SessionLocal())`.
- Changing the share or hold affects only bookings created afterwards. To stop the money for one booking, hold it.
- Roll back: `alembic downgrade -1` deletes the earning and compensation rows but leaves `caregiver_wallets.balance` unchanged, so after a downgrade the ledger no longer explains the balance (released money stays in the wallet with no row behind it). This is acceptable only because the project is greenfield with no real balances; once real money has been released, do not downgrade past `0011`, reconcile by hand or write a forward migration instead.

## Money-review fixes (same slice)
- **Late-cancel compensation honours the no-show flag.** `credit_late_cancel` now credits `held` for a booking with `no_show_flagged_at`, exactly as `credit_visit` does; both share `_no_show_hold` in `app/services/earnings.py`. Before, a flagged caregiver whose booking was then cancelled late by the family got 80% released after 24 hours.
- **`GET /caregiver/wallet` no longer races the release job.** A missing wallet is created with the same `INSERT ... ON CONFLICT DO NOTHING` as `_locked_wallet`, then re-selected; a concurrent insert used to be a 500.
- **Releasing a no-show hold needs `ledger.adjust`.** `release_hold(..., can_clear_no_show=...)` raises `EarningForbidden` (HTTP 403, "This booking is flagged as a no-show; releasing its hold needs the ledger.adjust permission") when the booking is still flagged and the caller lacks it. The route keeps `ledger.adjust_request` (so the RBAC matrix is unchanged) and passes `admin.has_permission("ledger.adjust")`; the check is in the service because it depends on the booking. Ordinary holds are still Finance's to release.
- **Earnings born held leave an audit row.** `credit_visit` / `credit_late_cancel` on a flagged booking write `earning.hold` (before none, after the held snapshot, attributed to the admin who flagged), the same action the admin hold endpoint writes.
- Tests: six added to `app/tests/test_caregiver_earnings.py` (flagged late cancel, audit rows, wallet insert race forced with a rival insert, release-hold permission both ways, ordinary hold unaffected).

## Follow-ups
- **Slice 2: done (2026-10-12)**, see [caregiver payouts](2026-10-12-caregiver-payouts.md): scheduled Paystack payouts using `payout_cadence` and `payout_min_naira`; `POST /caregiver/wallet/withdraw` now answers 410.
- Slice 3: **refunds queue done (2026-10-21)**, see [refunds v2](2026-10-21-refunds-v2.md): it enforces `finance_refund_limit_naira`. Ledger adjustments (AD-FR-42) to correct a released earning are still open.
- ~~A refund of a completed visit does not claw back~~ **Done (refunds v2, 2026-10-21):** the approver may take the caregiver's share back (`refunds.take_back_from_caregiver`). It follows what the caregiver received: an earning still `pending_clearance` or `held` is reduced in place (a `refund_take_back` row with status `voided` records the reduction); a released earning is debited from the wallet first and the rest is recorded as `caregiver_wallets.owed_kobo`; a voided or missing earning takes nothing. `earnings.release_due` nets `owed_kobo` off every earning it releases (each recovery is a `refund_take_back` row), so the caregiver is only ever credited less than the earning, never more. An earning for a booking cancelled after completion is not clawed back.
- Console screens for earnings.
- ~~**Refunds v2 (slice 3):** void or adjust the `late_cancel_compensation` earning when a late-cancel fee is later waived or refunded.~~ **Decided won't-do (owner, RF-D7, 2026-10-19):** the caregiver always keeps the compensation. The refund of its own booking never reverses it. A debt owed from another refund's take-back **is** recovered from a later release of a compensation (a ruling the owner has not confirmed; see the refunds v2 SOP follow-ups).
- **Product:** consider waiving the late-cancel fee for a no-show-flagged booking (today the family is charged and the compensation is held).
