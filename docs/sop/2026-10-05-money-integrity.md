# SOP: Money integrity (WP-D)

| | |
|---|---|
| **Date** | 2026-10-05 |
| **Branch** | `security/money` (base `test/harness`) |
| **Status** | Verified locally (71 tests green) |

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
| Refund is two-phase | Phase 1 under locks: request -> `processing`, deposit's wallet credit debited, pending refund tx, **commit**. Phase 2: Paystack call with no transaction open (15s timeout). Phase 3: success -> request `approved`, gateway id stored; definite rejection (Paystack 4xx / `status:false`) -> compensate (re-credit, refund tx `failed`, request back to `pending`, 502); ambiguous (timeout, network, 5xx) -> stays `processing`, logged for reconciliation, **never auto-retried**, 502 "outcome unknown" | Calling Paystack under row locks: a hung call holds locks and pool connections; auto-retrying an ambiguous call could refund twice |
| Refund states | Request: `pending` -> `processing` -> `approved` / back to `pending` / `failed` (Paystack later reported failure). Refund tx: `pending` -> `successful` (`refund.processed` webhook) / `failed` | |
| Refund webhooks | `refund.processed` marks the refund tx `successful`; `refund.failed` re-credits a reversed deposit once, marks tx `failed` and request `failed`. Matched by the Paystack refund id stored at phase 3. Idempotent; unknown ids ignored | |
| Refundable originals | Only `payment` / `deposit` transactions with status `successful`, checked at request and at approval (no refund of a refund) | |
| Kobo conversion | One `app.core.money.to_kobo` (Decimal, half-up) used by `initialize_transaction`, `initiate_refund` and settlement. Booking `total_amount` / `service_fee` / `platform_fee` rounded to 2dp at creation | Truncating on init vs rounding on check rejected real payments for fractional totals |
| Webhook key | Empty `PAYSTACK_SECRET_KEY` -> every webhook rejected (400), and startup fails when `ENVIRONMENT=production` | An empty key makes the HMAC forgeable by anyone |
| Webhook threading | `async` route only reads the body and verifies the signature; DB work runs in `run_in_threadpool` with its own session | Blocking `FOR UPDATE` on the event loop |
| Prescriptions | `charge.success` locks the row and requires paid kobo == `to_kobo(total_cost)`; replay no-op; `charge.failed` never touches a paid one | |
| Paystack timeouts | `timeout=15` on every `requests` call in `PaystackService` | |
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
- `app/core/money.py` (new): `to_kobo`. `app/core/config.py`: `ENVIRONMENT`, production key check. `app/services/paystack.py`: timeouts, `ambiguous` flag on refund errors, refund event parsing, empty-key rejection. `app/services/booking.py` `create_booking`: 2dp rounding.
- `app/services/booking_payment.py` (new): `settle_booking_payment`, `SettleOutcome`.
- `app/api/v1/endpoints/payment.py`: webhook uses the settlement helper; the raw signature is no longer logged.
- `app/api/v1/endpoints/booking.py` (`verify_booking_payment`).
- No migrations.

## Verification
`app/tests/test_money_withdraw.py`, `test_money_refunds.py`, `test_money_webhook.py`, `test_money_verify_payment.py`, `test_money_kobo_and_config.py`, `test_money_refund_flow.py` (66 tests). Each fix was written test-first and failed before the change. The two-phase test proves Paystack is called with no row lock held (`FOR UPDATE NOWAIT` from another session succeeds) and the committed state is already `processing` + debited. Highlights:
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
- A crash after phase 1 commit but before the Paystack call leaves a `processing` request that never reached Paystack; reconciliation is manual (query `patient_refund_requests.status='processing'`). Ambiguous outcomes land in the same bucket. No automated reconciler yet.
- Refund webhooks match on the Paystack refund id from the create response; the webhook payload shape follows Paystack docs and was not captured live. A `processing` request has no id and cannot be matched.
- Refund tx to request link is a `[refund_request:<id>]` tag in `description` (no migration); a proper `refund_request_id` column belongs with WP-E.
- Webhook signature uses `PAYSTACK_SECRET_KEY` (what Paystack signs with); `PAYSTACK_WEBHOOK_SECRET` is unused.
- Money columns are still Float/Numeric naira (WP-E2).
- Wallet payment endpoint does not link the payment to a booking status.
