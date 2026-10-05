# SOP: Money integrity (WP-D)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `security/money` (base `test/harness`) |
| **Status** | Verified locally (50 tests green) |

## What shipped
Five money-path fixes, each with tests that failed before the change:
1. Caregiver withdrawals are validated and race-free.
2. Refund approval pays exactly one destination and can't be approved twice.
3. The Paystack webhook is replay-safe and checks the paid amount.
4. `verify-payment` follows the same rules as the webhook and notifies caregivers once.
5. Wallet deposit/payment run as one locked transaction (nested `begin()` removed).

## Why
| Problem | Consequence |
|---|---|
| `amount` unchecked on withdraw | A negative amount credited the wallet; a double submit overdrew it |
| Refund approval credited the wallet **and** called Paystack | Patient paid twice; approving twice paid twice |
| Admin check was `user_type == "admin"` | Real admins (an `Admin` row) were locked out, a user with that type string was let in |
| Webhook re-marked bookings paid and re-notified on every replay; no amount check | Duplicate dispatches; a N1 payment could settle a N44,000 booking |
| `charge.failed` after `charge.success` cancelled a paid booking | Paid booking lost |
| `verify-payment` marked paid but never notified caregivers | Booking paid via polling was never dispatched |
| `self.db.begin()` inside an already-begun session | Raises under SQLAlchemy 2: wallet deposits and payments failed |

## How
| Decision | Choice | Alternative rejected |
|---|---|---|
| Withdraw validation | `amount` is `Field(gt=0)` (422 for zero/negative). Below `MIN_WITHDRAWAL_KOBO` (default 100000 = N1,000) is 400. Wallet read with `FOR UPDATE` + `populate_existing`. | Check-then-update with a `CHECK (balance >= 0)` only: still races the validation message |
| Refund destination | Original has a Paystack reference -> Paystack refund only; otherwise wallet credit only. Refund request row and original transaction row are locked. | Always crediting the wallet: ignores the card refund the spec wants |
| Deposit refunds to card | The deposit's wallet credit is **reversed** (balance debited) before the Paystack refund; blocked (409) if the balance can't cover it | Leaving the credit: the patient keeps the money in the wallet and also gets it on the card |
| Refund failure | Paystack failure -> 502, request stays `pending`, nothing written | Marking approved then retrying: leaves ledger/gateway out of sync |
| Cumulative cap | Sum of other approved refunds + this amount must be <= original amount (409) | Per-request cap only (what `request_refund` does): lets two 80% requests both pass |
| Booking settlement | One function, `settle_booking_payment`, used by webhook and verify-payment. Locks the booking, no-op if paid, compares kobo, only the caller that flips unpaid->paid notifies | Duplicating the logic in two endpoints |
| Amount mismatch record | `bookings.payment_failure_reason = "Amount mismatch: expected X kobo, paystack reported Y"`, booking left unpaid | A `payment_exceptions` table: heavier, needs a migration; revisit when ops needs a review queue |
| Mismatch response | Webhook returns 200 (so Paystack stops retrying); verify-payment returns 409 `{"detail": "Payment amount does not match booking total"}` | 4xx on the webhook: Paystack would retry forever |
| Paid cancelled booking | Marked paid, logged as an error, **not** re-dispatched | Reviving the booking silently |
| Deposit settlement | Locks the transaction row; replays and mismatches are no-ops; `charge.failed` only changes `pending` transactions | |

Error shape stays FastAPI's `{"detail": ...}`.

## What's involved
- `app/api/v1/endpoints/caregiver.py` (`request_withdrawal`), `app/schemas/caregiver.py` (`CaregiverWithdrawalRequest.amount`), `app/core/config.py` (`MIN_WITHDRAWAL_KOBO`).
- `app/services/patient_wallet.py`: `process_successful_payment`, `process_failed_payment`, `make_payment`, `process_refund_approval` (now returns `{"destination": ...}` and raises `RefundNotFound` / `RefundConflict` / `RefundGatewayError`), `verify_payment` (honours a rejected deposit).
- `app/api/v1/endpoints/patient.py`: admin refund routes use `get_current_admin`; 404 not found, 409 already processed / over-refund / wallet can't cover, 502 Paystack failure.
- `app/services/booking_payment.py` (new): `settle_booking_payment`, `SettleOutcome`.
- `app/api/v1/endpoints/payment.py`: webhook uses the settlement helper; the raw signature is no longer logged.
- `app/api/v1/endpoints/booking.py` (`verify_booking_payment`).
- No migrations.

## Verification
`app/tests/test_money_withdraw.py`, `test_money_refunds.py`, `test_money_webhook.py`, `test_money_verify_payment.py` (45 tests). 25 failed before the changes, all pass after. Highlights:
- Withdraw: negative, zero, below-minimum, overdraw; two parallel requests give exactly one 200; a deterministic lock test holds the wallet row in another session and proves the request waits, then re-reads the balance.
- Refunds: Paystack-only vs wallet-only, Paystack failure leaves it pending, second and concurrent approval, cumulative cap, 401/403, `Admin` row vs `user_type`.
- Webhook: bad/missing/foreign signature (real HMAC-SHA512), replay (sequential and 4 concurrent), wrong amount, failed-after-paid, deposit replay/mismatch.
- verify-payment: once-only notify, wrong amount, webhook+verify in either order, other patient 403.
- Paystack and `notify_nearby_caregivers` are mocked; the network guard stays on.

## Operate / roll back
- Config: `MIN_WITHDRAWAL_KOBO` (env override). No schema change, so rollback is a plain revert.
- Ops: grep logs for `amount mismatch` and `needs a refund review`; mismatches also sit in `bookings.payment_failure_reason`.

## Follow-ups
- A `payment_exceptions` review queue if ops needs more than the failure-reason column.
- Refund `pending` -> `successful` transition from the `refund.successful` webhook (currently only logged).
- The Paystack refund is called while holding the row locks; if the process dies between the Paystack call and commit, the refund is out with no ledger entry. A send-after-commit outbox would close that.
- Webhook signature uses `PAYSTACK_SECRET_KEY` (what Paystack signs with); `PAYSTACK_WEBHOOK_SECRET` is unused.
- Money columns are still Float/Numeric naira (WP-E2).
- Wallet payment endpoint does not link the payment to a booking status.
