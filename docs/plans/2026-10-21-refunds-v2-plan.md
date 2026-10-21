# Refunds v2 (A3 slice 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn every booking refund into one approved, exactly-once refund row: queue, approval limits, Paystack execution with webhooks and reconcile, failure handling, caregiver take-back, and the admin API the console will use.

**Architecture:** A new `app/services/refunds.py` owns the refund state machine. It is modelled on `app/services/payouts.py`: lock order booking → refund → ledger rows → wallet; Paystack is called only with no lock held, after a committed claim; an attempt-row history; from-state-guarded transitions; and contradictions surface as `needs_review` instead of moving money. The cancel paths create the rows through `flag_refund`. Admin routes live in `app/api/v1/endpoints/admin_refunds.py`. Caregiver take-back lives in `app/services/earnings.py`, under the payout invariant.

**Tech Stack:** FastAPI, SQLAlchemy 2 (sync sessions), Alembic, Postgres 16 (testcontainers in tests), ARQ cron jobs (`app/worker.py`), Paystack REST through `PaystackService._call`, pytest with `FakePaystack` (`app/tests/fake_meta.py`).

**Spec:** `docs/plans/2026-10-21-refunds-v2-design.md` (decisions RF-D1 to RF-D12). Read it first; section numbers below (§) refer to it.

## Global Constraints

- **Money:**
  - Store integer kobo everywhere; naira appears only in API schemas (`core/money.to_kobo` / `kobo_to_naira`).
  - Round percentages half-up: `(x * pct + 50) // 100`.
- **Lock order:** booking → refund → refund attempts → caregiver ledger rows → caregiver wallet / patient wallet.
  - Use `.with_for_update().populate_existing()`. Never call `.refresh()` after a lock.
  - Never call Paystack while holding a lock.
- **Paystack refunds are never resent on silence.** A timeout, 5xx or unreadable reply leaves the attempt `unknown`, and only reconcile settles it.
- **Payout invariant:** automatic code never credits a caregiver.
  - A take-back debits `min(balance, amount)` and records the rest in `caregiver_wallets.owed_kobo`.
  - `ck_caregiver_wallets_balance_not_negative` stays.
- **Open refunds:** at most one per booking (statuses `pending_approval`, `approved`, `processing`, `needs_attention`, `needs_review`, `failed`).
- **Refundable amount:** `refundable = total_amount_kobo − Σ amount_kobo of refunds in (approved, processing, needs_attention, failed, needs_review, succeeded)`.
- **Approval limit:** `finance_refund_limit_naira` from `booking_policy.current(db)`, cumulative per booking (RF-D12).
- **New permission `refunds.approve_above_limit`:** Super Admin only. It is needed:
  - above the limit;
  - for `destination="wallet"`;
  - for `switch-to-wallet` and `resolve`.
- **Staff-raised refunds** cannot be approved by their requester (RF-D11). The `403` message is exactly: `You raised this refund, so someone else must approve it.`
- **Paystack unreachable** during retry / switch-to-wallet checks: `503`, message exactly `We couldn't reach Paystack to confirm the last attempt failed. Try again shortly.`
- **The WhatsApp template `refund_update` `{{3}}` values**, exactly:
  - `being processed`
  - `complete`
  - `waiting for your bank details. Our team will contact you`
- **Migration:** `app/alembic/versions/0014_refunds_v2.py`, revision `0014_refunds_v2`, down_revision `0013_finance_audit_read`.
- **Commits:**
  - Clean commit messages with **no AI attribution**: no `Co-Authored-By`, no "Generated with".
  - No push and no merge: the coordinator pushes.
- **Tests:**
  - Run targeted tests with `poetry run pytest app/tests/<file> -q`.
  - Run the full suite only once, at the end (Task 10), after checking `pgrep -fl pytest` shows no other run.
- **Never read or print `backend/.env`.**

## Review Focus

1. **A webhook for a refund we don't know** (legacy wallet refund, a refund made in the Paystack dashboard, a booking with no open refund). Expect `200`, a log line and no state change. Test: `test_refund_event_with_no_open_refund_is_ignored` (Task 5).
2. **A `processed` webhook arriving after the refund was switched to wallet** (Paystack had in fact paid). Expect `needs_review` and an error log; nothing credited or reversed. Test: `test_processed_after_switch_to_wallet_goes_to_review` (Task 7).
3. **Approving while the booking is being cancelled or settled concurrently.** The refundable amount must be computed under the booking lock. Test: `test_two_approvals_cannot_exceed_total` (Task 9, a forced interleaving).
4. **A take-back on a caregiver with a payout already reserved.** The reserved payout must stay untouched, and the shortfall must go to `owed_kobo`. Test: `test_take_back_never_touches_a_reserved_payout` (Task 6).
5. **A booking with `paystack_fee_kobo` null** (settled before this slice) shown in the detail. Expect `paystack_fee_naira: null`, never `0`. Test: `test_detail_shows_unknown_paystack_fee_as_null` (Task 8).

---

### Task 1: Base on 0013, migration, models, permission

**Files:**
- Git: rebase the branch.
- Create: `app/alembic/versions/0014_refunds_v2.py`, `app/db/models/refund.py`
- Modify:
  - `app/db/models/booking.py` (`PaymentStatus.PARTIALLY_REFUNDED`, `paystack_fee_kobo`)
  - `app/db/models/caregiver.py` (`owed_kobo`, type CHECK plus `refund_take_back`, constant `TX_REFUND_TAKE_BACK`)
  - `app/db/models/patient.py` (`PatientTransaction.refund_id` nullable FK)
  - `app/db/models/__init__.py`
  - `app/core/admin_permissions.py` (add `refunds.approve_above_limit` to the permission list. Super Admin gets it automatically because it holds every permission; do not add it to any other role)
- Test: `app/tests/test_refunds_migration.py`, `app/tests/test_admin_rbac.py` (extend)

**Interfaces:**
- Produces:
  - `Refund` (table `refunds`) and `RefundAttempt` (table `refund_attempts`) in `app/db/models/refund.py`, with the columns of spec §4.1 and §4.2.
  - String constants `REFUND_OPEN_STATUSES`, `REFUND_COMMITTED_STATUSES` (the statuses counted in "refundable") and `REFUND_STATUSES`.
  - `Refund.attempts` relationship ordered by `attempt`.
  - `Refund.current_attempt` property: the highest attempt or `None`.

- [ ] **Step 1: Stack on 0013.**
  - Run `git fetch oakly`.
  - If `oakly/main` contains `app/alembic/versions/0013_finance_audit_read.py`, run `git rebase oakly/main`.
  - Otherwise run `git rebase oakly/feat/finance-audit-read` and record in `LEDGER-refunds-v2.md` that the branch is stacked on PR #18.
  - Expected: `ls app/alembic/versions | tail -1` shows `0013_finance_audit_read.py`.
- [ ] **Step 2: Write the failing migration tests** in `test_refunds_migration.py`:
  - `test_upgrade_downgrade_round_trip`: `alembic upgrade head`, then `downgrade -1`, then `upgrade head`; the tables `refunds` and `refund_attempts` exist after the final upgrade.
  - `test_one_open_refund_per_booking`: a second `pending_approval` row for the same booking raises `IntegrityError`. A `succeeded` plus a `pending_approval` row for one booking is allowed.
  - `test_refund_amount_must_be_positive`: `amount_kobo=0` raises `IntegrityError`.
  - `test_owed_kobo_cannot_go_negative`
  - `test_payment_status_accepts_partially_refunded`
  - `test_ledger_accepts_refund_take_back_type`
  - In `test_admin_rbac.py`, `test_only_super_admin_can_approve_above_limit`: the role map gives `refunds.approve_above_limit` to `super_admin` only.
- [ ] **Step 3: Run them.** `poetry run pytest app/tests/test_refunds_migration.py app/tests/test_admin_rbac.py -q`. Expected: FAIL (no tables, no permission).
- [ ] **Step 4: Write the models and the migration.** Follow the style of `0012_caregiver_payouts.py`: a docstring, named CHECKs, `pg_insert` style.
  - **Enum:** `ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'PARTIALLY_REFUNDED'` inside `op.get_context().autocommit_block()`. The enum stores **names** (uppercase), as in `0001_baseline.py:206`.
  - **Downgrade:** drop the tables, columns and CHECKs. Leave the enum value in place (Postgres cannot drop one) and say so in the docstring.
  - **Partial unique index:** `uq_refunds_one_open_per_booking` on `refunds(booking_id)` where status is in the open set.
  - **Unique** `refund_attempts(refund_id, attempt)`.
  - **Index** `refund_attempts(paystack_refund_id)`.
- [ ] **Step 5: Run the tests again.** Expected: PASS.
- [ ] **Step 6: Commit.** `feat(refunds): refund tables, partially refunded status, caregiver owed balance (migration 0014)`

### Task 2: Paystack edge: refund API, fake, Paystack fee on settlement

**Files:**
- Modify:
  - `app/services/paystack.py`
  - `app/tests/fake_meta.py` (`FakePaystack`)
  - `app/services/booking_payment.py` (`settle_booking_payment`, `verify_and_settle`)
  - `app/api/v1/endpoints/payment.py` (pass `fees` through)
- Test: `app/tests/test_paystack_refund_client.py`, `app/tests/test_booking_payment.py` (extend, or the existing settlement test file)

**Interfaces:**
- Produces, on `PaystackService`:
  - `create_refund(transaction_reference: str, amount_kobo: int) -> Dict` returns `{"outcome": "accepted"|"refused"|"unknown", "paystack_refund_id": Optional[str], "status": Optional[str], "message": Optional[str]}`. It goes through `_call`. A 4xx is `refused`; a timeout, 5xx or unreadable body is `unknown`. Leave the old `initiate_refund` in place: the legacy code is retired in Task 8.
  - `fetch_refund(paystack_refund_id: str) -> Dict` returns `{"outcome": "found"|"not_found"|"unknown", "status": Optional[str], "amount_kobo": Optional[int], "message": Optional[str]}`.
  - `list_refunds(transaction_reference: str) -> Dict` returns `{"outcome": "found"|"unknown", "refunds": List[{"paystack_refund_id", "status", "amount_kobo"}]}`.
  - `retry_refund_with_customer_details(paystack_refund_id: str, account_number: str, bank_id: str) -> Dict`, with the same shape as `create_refund`.
- Produces, on `FakePaystack`:
  - Routes `POST /refund`, `GET /refund/{id}`, `GET /refund?transaction=`, `POST /refund/retry_with_customer_details/{id}`.
  - `fake.refunds` (a dict by id).
  - `fake.settle_refund(paystack_refund_id: str, status: str) -> dict`, which returns the signed-ready webhook body shaped like Paystack's documented sample: `event: "refund.<status>"`, `data.transaction_reference`, `data.refund_reference: null`, `data.amount` as a string, and the id in `data.id`.
  - `fake.refund_error` (forces `"timeout"` / `"500"` / `"422"`).
- Produces: `settle_booking_payment(..., fees_kobo: Optional[int] = None)` stores `bookings.paystack_fee_kobo` when given.

- [ ] **Step 1: Write the failing tests.**
  - `test_create_refund_accepted_returns_id`
  - `test_create_refund_422_is_refused_with_message`
  - `test_create_refund_timeout_is_unknown_never_refused`
  - `test_fetch_refund_not_found`
  - `test_list_refunds_by_transaction`
  - `test_retry_with_customer_details_only_after_needs_attention`: the fake answers `422` otherwise, as Paystack does.
  - `test_charge_success_records_paystack_fee`: the webhook body has `data.fees = 66000`, so the booking gets `paystack_fee_kobo == 66000`.
  - `test_charge_without_fees_leaves_fee_null`
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_paystack_refund_client.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement the four client methods, the fake routes and helpers, and the fee pass-through.** In `payment.py`, read `data.fees` (int kobo) from `charge.success` and from the verify response.
- [ ] **Step 4: Run them again.** Expected: PASS. Also run `poetry run pytest app/tests/test_payouts.py -q` to prove the fake still serves payouts.
- [ ] **Step 5: Commit.** `feat(paystack): refund create/fetch/list/retry calls, fake refunds, record the Paystack fee`

### Task 3: Refund creation, suggestions, and the app caregiver-cancel gap

**Files:**
- Create: `app/services/refunds.py` (this task: the creation half)
- Modify:
  - `app/services/booking_cancellation.py` (`flag_refund`)
  - `app/services/booking.py` (`_after_caregiver_cancel`)
  - `app/services/admin_bookings.py` (the cancel path keeps calling `flag_refund`; nothing new besides the reason code)
- Test: `app/tests/test_refunds_create.py`

**Interfaces:**
- Produces, in `app/services/refunds.py`:
  - Exceptions `RefundNotFound` (→404), `RefundConflict` (→409), `RefundForbidden` (→403), `RefundInvalid` (→422) and `RefundPaystackUnavailable` (→503). Each carries `.message`.
  - `refundable_kobo(db: Session, booking: Booking) -> int`
  - `suggested_amount_kobo(booking: Booking, reason_code: str, entered_kobo: Optional[int]) -> Optional[int]`, implementing spec §5 from the booking's frozen `cancel_fee_tier` / `cancel_policy`.
  - `open_system_refund(db: Session, booking: Booking, reason_code: str, entered_kobo: Optional[int]) -> Optional[Refund]`:
    - It does not commit; it runs in the caller's transaction, with the booking already locked.
    - It returns `None` if the booking is not `PAID` or an open refund exists.
    - Otherwise it creates the row (`origin="system"`, `pending_approval`) and clears `booking.refund_required` (the queue row replaces the flag).
  - `request_refund(db: Session, *, booking_id: UUID, amount_kobo: int, reason_code: str, note: Optional[str], admin: AdminUser, request: Optional[Request] = None) -> Refund`:
    - It commits and writes the audit action `refund.request`.
    - It returns `409` if an open refund exists and `422` if `amount > refundable` or the booking is not paid.
- Reason codes, exactly: `cancelled_by_booker`, `caregiver_cancelled`, `no_caregiver_found`, `admin_cancelled`, `paid_after_expiry`, `paid_after_cancellation`, `complaint`, `other`.
- `flag_refund` maps its existing reasons onto these codes: `admin_cancelled_full` and `admin_cancelled_partial` both become `admin_cancelled`.

- [ ] **Step 1: Write the failing tests.** Totals use `make_booking(total_amount_kobo=4_400_000)` with `service_fee_kobo=4_000_000`.
  - `test_family_cancel_standard_tier_suggests_total_minus_20pct_of_service_fee`: suggests `3_600_000`.
  - `test_family_cancel_late_tier_suggests_total_minus_50pct`: suggests `2_400_000`.
  - `test_free_tier_and_no_fault_suggest_every_naira`: suggests `4_400_000` for `caregiver_cancelled`, `no_caregiver_found`, `admin_cancelled` full and `paid_after_expiry`.
  - `test_paystack_fee_is_never_deducted`: with `paystack_fee_kobo=66_000`, the suggestion is still the total minus the tier fee.
  - `test_started_visit_dropout_has_no_suggestion`: suggests `None`.
  - `test_whatsapp_cancel_by_booker_creates_pending_refund`: goes through `cancel_by_booker`; asserts one `Refund` with `origin="system"` and `refund_required` cleared.
  - `test_flag_refund_twice_creates_one_refund`
  - `test_unpaid_booking_creates_no_refund`
  - `test_app_caregiver_cancel_of_paid_booking_queues_full_refund`: the gap. A channel `app`, `ACCEPTED`, `PAID` booking cancelled through the caregiver "cancel" action gets a `Refund` with `reason_code="caregiver_cancelled"` and `suggested_amount_kobo == total`.
  - `test_whatsapp_caregiver_dropout_still_redispatches`: unchanged behaviour; no refund is created.
  - `test_staff_request_rejects_more_than_refundable`, `test_staff_request_conflicts_with_open_refund`
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_refunds_create.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `flag_refund` keeps its signature and also calls `open_system_refund`.
  - `_after_caregiver_cancel` flags a full refund (`caregiver_cancelled`, total) for a non-WhatsApp `PAID` booking, instead of returning early. The WhatsApp branch is untouched.
- [ ] **Step 4: Run the tests again**, then the regression set: `poetry run pytest app/tests/test_refunds_create.py app/tests/test_cancellation_tiers.py app/tests/test_caregiver_cancel_window.py app/tests/test_admin_bookings.py -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(refunds): queue a refund from every cancel path, including app caregiver cancels`

### Task 4: Approve and reject (money path, Opus review)

**Files:**
- Modify: `app/services/refunds.py`, `app/services/patient_wallet.py` (add one locked helper)
- Test: `app/tests/test_refunds_approve.py`

**Interfaces:**
- Consumes: Task 3's `refundable_kobo` and the exceptions.
- Produces:
  - `approve_refund(db: Session, refund_id: UUID, *, admin: AdminUser, amount_kobo: Optional[int], destination: str = "original_method", take_back_from_caregiver: bool = False, take_back_reason: Optional[str] = None, note: Optional[str] = None, request: Optional[Request] = None) -> Refund`
  - `reject_refund(db: Session, refund_id: UUID, *, admin: AdminUser, reason: str, request: Optional[Request] = None) -> Refund`
  - `take_back_preview_kobo(db: Session, refund: Refund, amount_kobo: int) -> int`: the §7 formula, `0` when the booking is not completed or has no `earning` row.
  - `patient_wallet.credit_refund_locked(db: Session, patient_id: UUID, amount_kobo: int, refund_id: UUID) -> PatientTransaction`:
    - It locks the patient wallet, credits `balance_kobo` and `total_refunded_kobo`, and writes a `PatientTransaction(type="refund", status="successful", refund_id=...)`.
    - It does not commit.
  - `_mark_succeeded(db, refund: Refund, booking: Booking, now: datetime) -> None`: the single success routine, also used by Tasks 5 and 7. It:
    - sets `succeeded` and `processed_at`;
    - sets booking `payment_status` to `REFUNDED` when Σ succeeded equals the total, otherwise `PARTIALLY_REFUNDED`;
    - calls `earnings.apply_take_back(...)` when `take_back_kobo > 0` (Task 6 provides it; until then it is a no-op stub raising `NotImplementedError` only if `take_back_kobo > 0`);
    - emits the `refund_update` "complete" event.

- [ ] **Step 1: Write the failing tests.** Each one ends with `assert_refund_conserved(db, booking)` (new helper in `app/tests/refund_helpers.py`), which asserts `Σ committed refunds ≤ total`.
  - `test_finance_approves_up_to_limit`: ₦50,000 exactly is allowed.
  - `test_finance_above_limit_is_403_and_names_the_limit`
  - `test_limit_is_cumulative_per_booking`: a ₦30,000 refund has succeeded, so a new ₦25,000 refund by Finance gets `403`.
  - `test_super_admin_approves_above_limit`
  - `test_requester_cannot_approve_own_staff_refund`: `403` with the exact message. Another Finance user is allowed.
  - `test_system_refund_any_finance_can_approve`
  - `test_amount_defaults_to_suggestion_and_null_suggestion_requires_amount`: `422`.
  - `test_amount_above_refundable_is_422`
  - `test_wallet_destination_needs_super_admin_and_an_app_booker`: Finance gets `403`, and a WhatsApp-only booker gets `422`.
  - `test_wallet_destination_credits_once_and_succeeds`: the patient wallet balance goes up by the amount, there is one `PatientTransaction` with `refund_id`, and the booking becomes `REFUNDED` / `PARTIALLY_REFUNDED`.
  - `test_take_back_only_for_completed_and_needs_reason`: `422` otherwise.
  - `test_approve_twice_is_409`
  - `test_reject_needs_reason_and_is_terminal`
  - `test_every_action_writes_an_audit_row`: `refund.approve` / `refund.reject`, with amount, destination, take-back and limit in `after`.
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_refunds_approve.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Lock the booking, then the refund. Check the state, the amount, then the limit: `limit_kobo = policy.finance_refund_limit_naira * 100`, compared against `amount + committed for this booking`.
  - `can_above = "refunds.approve_above_limit" in admin.permissions`.
  - For `original_method`, set `status="approved"` and the audit row, then commit. For `wallet`, credit the wallet, call `_mark_succeeded`, then commit.
- [ ] **Step 4: Run the tests again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(refunds): approve and reject with limits, one destination, requester rule`
- [ ] **Step 6: Opus review of Task 4.** Focus: limit bypasses, double credit, lock order, requester rule. Fix the findings, re-run, commit `fix(refunds): <finding>`.

### Task 5: Send job, webhook and reconcile (money path, Opus review)

**Files:**
- Modify:
  - `app/services/refunds.py`
  - `app/worker.py` (`refund_dispatch_job` at `second=40`; `refund_reconcile_job` at minute `{7,22,37,52}`)
  - `app/api/v1/endpoints/payment.py` (route `refund.*` events to `refunds.apply_refund_event`)
- Test: `app/tests/test_refunds_settle.py`

**Interfaces:**
- Consumes: `PaystackService.create_refund`, `fetch_refund`, `list_refunds` (Task 2), `_mark_succeeded` (Task 4).
- Produces:
  - `dispatch_due(db: Session, *, paystack: Optional[PaystackService] = None, limit: int = 20, now: Optional[datetime] = None) -> int`
  - `apply_refund_event(db: Session, *, status: str, transaction_reference: str, paystack_refund_id: Optional[str], amount_kobo: Optional[int], reason: Optional[str] = None, now: Optional[datetime] = None) -> str`. It returns one of `"applied"`, `"duplicate"`, `"ignored"` or `"review"`. `status` is the Paystack refund status: `pending`, `processing`, `needs-attention`, `processed` or `failed`.
  - `reconcile(db: Session, *, paystack: Optional[PaystackService] = None, now: Optional[datetime] = None) -> int`
  - Constants: `RECONCILE_AFTER = timedelta(minutes=10)` and `NOT_FOUND_GRACE = timedelta(minutes=30)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_send_claims_commits_then_calls_paystack`: the attempt row is committed with status `sending` before the fake sees the request (assert this inside a fake hook).
  - `test_two_workers_send_once`: two `dispatch_due` calls with `SKIP LOCKED` produce exactly one `POST /refund`.
  - `test_accepted_send_moves_to_processing_and_notifies_being_processed`
  - `test_refused_send_fails_with_paystack_message`
  - `test_timeout_send_is_unknown_and_never_resent`: `dispatch_due` runs 3 times and the fake sees 1 request.
  - `test_processed_webhook_succeeds_once`: the same body is sent twice; the second returns `"duplicate"`. The booking becomes `REFUNDED`, and the family gets `refund_update` "complete" once.
  - `test_partial_processed_sets_partially_refunded`
  - `test_needs_attention_webhook_notifies_bank_details`
  - `test_failed_webhook_records_reason`
  - `test_webhook_matched_by_transaction_reference_when_refund_reference_null`
  - `test_amount_mismatch_goes_to_review`
  - `test_processed_for_old_attempt_goes_to_review`
  - `test_refund_event_with_no_open_refund_is_ignored` (Review Focus 1)
  - `test_reconcile_settles_unknown_from_list_refunds`
  - `test_reconcile_no_record_after_grace_goes_to_review_never_failed`
  - `test_webhook_endpoint_routes_refund_events`: a signed body through `POST /payment/paystack/webhook` returns 200 and applies the event.
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_refunds_settle.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Match** by `paystack_refund_id`, else by `transaction_reference` → the booking with that `paystack_reference` → its open refund → its current attempt.
  - **Transitions are from-state guarded:**
    - `processed` succeeds only from `processing` / `needs_attention`.
    - A repeat returns `duplicate`.
    - Any other combination returns `review`: set `needs_review` and log an error.
  - **Notifications:** via `NotificationService(db)` events `refund_flagged` ("being processed"), `refund_processed` ("complete") and a new event `refund_needs_bank_details`.
    - Map all three to the `refund_update` template in `app/channels/whatsapp/booking_notices.py`, with the exact `{{3}}` strings.
    - Key each by `f"{refund.id}:{status}"`.
- [ ] **Step 4: Run the tests again.** Expected: PASS. Also run `poetry run pytest app/tests/test_payouts.py app/tests/test_whatsapp_notifications*.py -q` (transfer routing and templates unchanged).
- [ ] **Step 5: Commit.** `feat(refunds): send job, refund webhooks and reconcile, never resending on silence`
- [ ] **Step 6: Opus review of Task 5.** Focus: resend paths, webhook/reconcile races, mismatches, duplicates. Fix, re-run, commit.

### Task 6: Caregiver take-back and owed recovery (money path, Opus review)

**Files:**
- Modify: `app/services/earnings.py` (`apply_take_back`, `release_due`), `app/services/refunds.py` (remove the stub)
- Test: `app/tests/test_refunds_take_back.py`

**Interfaces:**
- Produces: `earnings.apply_take_back(db: Session, *, caregiver_id: UUID, booking_id: UUID, refund_id: UUID, amount_kobo: int, now: datetime) -> Tuple[int, int]`. It returns `(debited_kobo, owed_added_kobo)`.
  - It locks the wallet with `locked_wallet` and debits `min(balance, amount)`, writing a `refund_take_back` row (`successful`, `status_reason=f"refund {refund_id}"`).
  - It adds the rest to `owed_kobo`.
  - It does not commit.
- Modifies: `release_due` releases each earning by crediting `amount − min(amount, owed_kobo)`. It lowers `owed_kobo` by what it took and writes a `refund_take_back` row for that part, all under the existing wallet lock.

- [ ] **Step 1: Write the failing tests.** Each ends in `assert_never_over(db, cg, earned_kobo)` (existing `payout_helpers`), extended to subtract take-backs from `earned` and add `owed_kobo` to what is not yet recovered.
  - `test_take_back_is_proportional`: earning `3_200_000` on a total of `4_400_000`, refund `2_200_000`, so the take-back is `1_600_000`.
  - `test_take_back_capped_by_earlier_take_backs_for_the_booking`
  - `test_take_back_from_wallet_first_then_owed`: balance `1_000_000`, take-back `1_600_000`, so the debit is `1_000_000` and owed is `600_000`; the balance is `0`.
  - `test_owed_is_recovered_from_the_next_release`
  - `test_take_back_never_touches_a_reserved_payout` (Review Focus 4)
  - `test_take_back_on_unreleased_earning_recovers_at_release`
  - `test_late_cancel_compensation_is_never_taken_back` (RF-D7)
  - `test_take_back_applies_only_on_success`: a refund approved with take-back that then fails and is closed takes nothing.
  - `test_take_back_does_not_freeze_payouts`
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_refunds_take_back.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests again**, then `poetry run pytest app/tests/test_caregiver_earnings.py app/tests/test_payout_hardening.py -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(earnings): take a refunded visit's share back, wallet first, then from future earnings`
- [ ] **Step 6: Opus review of Task 6.** Focus: the invariant, `release_due` with `SKIP LOCKED`, the interplay with payout `_reserve`. Fix, re-run, commit.

### Task 7: Failure actions: retry, bank details, switch to wallet, close, resolve (money path, Opus review)

**Files:**
- Modify: `app/services/refunds.py`
- Test: `app/tests/test_refunds_failures.py`

**Interfaces:**
- Produces (each locks booking → refund → attempts, writes an audit row `refund.<action>`, and commits):
  - `retry_refund(db, refund_id, *, admin, paystack=None, request=None) -> Refund`
  - `submit_bank_details(db, refund_id, *, admin, bank_code: str, account_number: str, paystack=None, request=None) -> Refund`
  - `switch_to_wallet(db, refund_id, *, admin, paystack=None, request=None) -> Refund`
  - `close_refund(db, refund_id, *, admin, reason: str, request=None) -> Refund`
  - `resolve_refund(db, refund_id, *, admin, outcome: str, note: str, request=None) -> Refund`, with `outcome` being `succeeded` or `failed`.
- The Paystack check for retry and switch runs **before** any lock, then is **re-read under the locks**. If the attempt changed in between, return `409`.

- [ ] **Step 1: Write the failing tests.**
  - `test_retry_only_from_failed_after_paystack_confirms_failed`
  - `test_retry_when_paystack_says_processed_settles_and_refuses`
  - `test_retry_when_paystack_unreachable_is_503_and_stays_failed`
  - `test_retry_creates_attempt_two_and_keeps_attempt_one`
  - `test_bank_details_resolves_name_calls_retry_and_stores_only_last4`: the full account number appears nowhere in DB rows or audit JSON.
  - `test_bank_details_only_from_needs_attention`
  - `test_switch_to_wallet_super_admin_only_after_confirmed_failure`
  - `test_processed_after_switch_to_wallet_goes_to_review` (Review Focus 2)
  - `test_close_needs_reason_and_allows_a_new_refund_later`
  - `test_resolve_succeeded_applies_success_effects_once`
  - `test_resolve_failed_returns_to_failed`
  - `test_resolve_needs_super_admin`
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_refunds_failures.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(refunds): retry, bank details, switch to wallet, close and resolve`
- [ ] **Step 6: Opus review of Task 7.** Focus: check-then-act races and the wallet switch double payment. Fix, re-run, commit.

### Task 8: Admin API, retiring the legacy routes

**Files:**
- Create: `app/api/v1/endpoints/admin_refunds.py`, `app/schemas/admin_refunds.py`
- Modify:
  - `app/api/v1/api.py` (include the router)
  - `app/api/v1/endpoints/patient.py` (the three legacy routes return `410`)
  - `app/services/patient_wallet.py` (`process_refund_webhook` no longer routed)
  - `scripts/ci_smoke.py` if it lists operations
- Test: `app/tests/test_admin_refunds_api.py`

**Interfaces:**
- Consumes: every service function from Tasks 3–7. Exceptions map to status codes as listed in Task 3, with `{"detail": message}`.
- Produces:
  - The routes of spec §8, exactly as tabled.
  - Response model `RefundOut` with:
    - `id`, `booking_id`, `status`, `reason_code`, `origin`;
    - `suggested_amount_naira`, `amount_naira`, `destination`;
    - `take_back_from_caregiver`, `take_back_naira`;
    - `failure_reason`, `requested_by`, `approved_by`;
    - `created_at`, `approved_at`, `processed_at`;
    - `available_actions: List[str]`.
  - The detail adds a `booking` block:
    - `status`, `channel`, `cancel_fee_tier`, `cancel_fee_naira`;
    - `caregiver_assigned`, `caregiver_arrived`, `completed`;
    - `total_naira`, `paystack_fee_naira`, `refunded_naira`, `refundable_naira`.
  - It also adds `attempts`, `take_back_preview_naira`, `needs_super_admin` and `limit_naira`.
  - The list is paged like `/admin/payouts`, with the same `page` / `page_size` / `total` names.
  - `available_actions` values: `approve`, `reject`, `retry`, `bank_details`, `switch_to_wallet`, `close`, `resolve`. Compute them from the state and `admin.permissions`, and leave `approve` out for the requester of a staff refund.
  - The legacy `410` messages:
    - `Refunds are now handled by the Oakly team. Contact support to request one.` (patient)
    - `This route has moved to /api/v1/admin/refunds.` (admin)

- [ ] **Step 1: Write the failing tests.**
  - **Permission matrix:** `test_permissions_per_route`, parametrized over each role × route, with expected 200/403.
  - **Validation:**
    - `test_list_defaults_to_open_and_filters`
    - `test_detail_shows_unknown_paystack_fee_as_null` (Review Focus 5)
    - `test_available_actions_hide_approve_for_requester`
    - `test_naira_in_and_out`: `amount_naira: 35000.5` becomes `3_500_050` kobo.
    - `test_approve_body_validation_422s`
  - **Legacy:**
    - `test_legacy_routes_answer_410`
    - `test_legacy_refund_webhook_no_longer_touches_wallet`
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_admin_refunds_api.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Thin routes: `Depends(require_permission(...))`, where a route open to any of several permissions passes them all to `require_permission`.
- [ ] **Step 4: Run the tests again**, then `poetry run pytest app/tests/test_admin_rbac.py app/tests/test_money_refunds.py app/tests/test_money_refund_flow.py app/tests/test_money_refund_stuck.py -q`. Update the legacy tests: they now assert `410`, and the old flow tests are deleted with a line in the SOP. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(admin): refunds API for the console; retire the legacy refund routes`

### Task 9: Sequence test and races (money path, Opus review)

**Files:**
- Create: `app/tests/test_refunds_sequences.py`
- Modify: `app/tests/refund_helpers.py`

- [ ] **Step 1: Write the tests.**
  - `test_no_event_sequence_refunds_more_than_was_paid`:
    - **Steps:** 100 seeded sequences, each up to 14 steps over one booking, picked from: create (system / staff), approve (random amount and destination, with or without take-back), send, webhook (any status, any attempt, duplicates, out of order), reconcile (found, not found, unknown), retry, bank details, switch, close, resolve.
    - **After every step, assert:**
      1. Σ committed refunds ≤ total paid.
      2. Paystack-processed amount plus wallet credits ≤ total paid.
      3. At most one open refund.
      4. The caregiver check from `assert_never_over`.
      5. A `needs_review` refund moved no money after it was flagged.
    - **Coverage:** at least 1 review and 1 wallet switch per run.
  - Race tests, using a forced interleaving with two sessions and a `threading.Event`, as in `test_payout_hardening.py`:
    - `test_two_approvals_cannot_exceed_total` (Review Focus 3)
    - `test_webhook_and_reconcile_settle_once`
    - `test_switch_to_wallet_racing_a_processed_webhook`
    - `test_take_back_racing_release_due_and_a_payout_batch`
- [ ] **Step 2: Run them.** `poetry run pytest app/tests/test_refunds_sequences.py -q`. Expected: PASS. If a test fails, it is a real bug: fix it in the owning service under `superpowers:systematic-debugging`, then re-run.
- [ ] **Step 3: Mutation-check two guards.**
  - Remove the from-state guard on `processed`; the sequence test must fail.
  - Remove the `refundable` check in approve; `test_two_approvals_cannot_exceed_total` must fail.
  - Restore both.
- [ ] **Step 4: Commit.** `test(refunds): random event sequences and forced races`
- [ ] **Step 5: Opus final whole-branch money review (round 2+).** Fix, re-run the refunds tests, commit.

### Task 10: SOP, checklist, FE guide from live captures, full local CI

**Files:**
- Create: `docs/sop/2026-10-21-refunds-v2.md`, plus capture files under `docs/fe-captures/` (follow the existing numbering)
- Modify:
  - `docs/checklist/MASTER_CHECKLIST.md`
  - `docs/fe-integration-guide-admin.md` (or its source template, if `render_fe_guide` renders it)
  - `docs/sop/2026-10-07-caregiver-earnings.md` (follow-ups now done or decided)
  - `LEDGER-refunds-v2.md`

- [ ] **Step 1: Run live captures** on the shared compose stack, only when the coordinator confirms it is free (ports 8001 / 5433 / 6380; `alembic upgrade head`), with the fake Paystack served by `serve_fake_paystack`, and label it as the fake. Capture:
  - the list and the detail;
  - staff request;
  - approve as Finance (≤ limit), Finance above the limit (`403`), Super Admin above the limit, the requester `403`;
  - wallet destination;
  - send → processed webhook → detail;
  - needs-attention → bank details;
  - failed → retry, failed → switch to wallet, close, resolve;
  - legacy `410`s.

  Paste the bodies verbatim. Mark as **not live**: real Paystack, real refund timing (up to 10 business days), and WhatsApp delivery. Run `down` / clean up afterwards.
- [ ] **Step 2: Write the SOP** in the payouts-SOP style: what shipped, why, how (with the decision table RF-D1..D12), what's involved, verification, operate / roll back (Postgres keeps the enum value on downgrade), follow-ups. The follow-ups are:
  - app patients' refund notifications;
  - showing `owed_kobo` in the caregiver app;
  - `/wallet/payment` with a `booking_id`;
  - deposit refunds;
  - the webhook-exceptions queue;
  - console screens.
- [ ] **Step 3: Update the checklist.**
  - Tick the Refunds v2 items, with the branch and SHA.
  - Mark "void/adjust `late_cancel_compensation`" as decided won't-do (RF-D7).
  - Tick the app caregiver-cancel gap.
  - Add the follow-ups under deferred.
- [ ] **Step 4: Reproduce CI locally** from `.github/workflows/ci.yml` with the pinned toolchain:
  - `poetry check --lock`
  - `poetry run ruff check --select E9,F63,F7,F82 .`
  - `scripts.ci_smoke` with CI's dummy env
  - both `render_fe_guide ... --check`
  - then, after `pgrep -fl pytest` is empty, the full `poetry run pytest` (about 18 min, run in the background)

  Expected: all green. Record the counts in the SOP and the ledger.
- [ ] **Step 5: Commit** `docs(refunds): SOP, checklist and admin FE guide from live captures`, and check `git log` for the absence of any attribution trailer.
