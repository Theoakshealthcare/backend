"""Caregiver payouts (A3 slice 2): a scheduled batch of Paystack bank transfers, approved by an admin.

The flow, all integer kobo (``caregiver_wallets.balance`` is the AVAILABLE balance, see ``app/db/models/payout.py``):

1. **Create.** On a payout day (``payout_cadence`` in the booking policy: weekly = Mondays, biweekly = Mondays of even ISO
   weeks, monthly = the 1st) the ARQ job ``create_scheduled_batch`` makes ONE batch (``period_key`` is the date, unique).
   For every caregiver whose available balance is at least ``payout_min_naira`` and who has a verified bank account (a
   Paystack recipient), is not suspended or erased, is not in the 24 hour cool-down after changing bank account and has no
   live payout, it takes the **whole available balance out of the wallet** under the wallet lock and writes it as a
   ``payout`` debit (``pending_approval``) plus one ``payouts`` row. What a refund take-back could not take earlier
   (``caregiver_wallets.owed_kobo``) is netted off first: the debt is recovered from the balance before anything is reserved
   (a ``refund_take_back`` row), which only DECREASES what the caregiver can be paid, so it fits the invariant below. A retry of
   a failed payout is refused while a debt is owed, and the next payout run takes it first. The reservation is the debit: the balance and the
   ledger always agree, and the same money cannot be reserved twice.
2. **Approve.** An admin approves the batch with a reason. Each payout is checked again (suspended since? account changed?);
   one that fails the check is cancelled and its money returned. The rest become ``processing``.
3. **Send.** The ARQ job ``dispatch_due`` claims each processing payout (``initiated_at``) after checking the caregiver is
   still approved, active and not erased (if not, the payout is cancelled and the money returned), then calls Paystack
   ``POST /transfer`` with the payout's own ``reference`` with no lock held. A definite refusal (for example Paystack's own
   balance is too low) fails the payout and returns the money; no answer leaves it ``processing``.
4. **Settle.** ``transfer.success`` / ``transfer.failed`` / ``transfer.reversed`` webhooks (``apply_transfer_event``), or the
   reconcile job asking Paystack (``reconcile``) when a webhook never came, settle it. Failed and reversed payouts return
   the money to the available balance. Every transition is idempotent: it happens from one state only. Events are matched
   by the reference of the ATTEMPT (``payout_attempts``), so a late event for an earlier attempt is not lost.

**The invariant.** Once a caregiver's payouts are disputed, automatic code may only DECREASE what the caregiver can be paid,
never increase it. Money returns only through an explicit, audited admin action. The dispute is a *contradiction*: Paystack's
word disagrees with what we recorded. Those are

* a success after the payout failed, was reversed or was cancelled (the money had already been returned),
* a success on an attempt that is not the current one, or on a payout that was never sent, or a second success,
* a success for a different amount than the payout's (the payout is flagged ``needs_attention`` whatever its status, and no
  money moves),
* the reconcile job finding Paystack has no record of a transfer we sent,
* a reversal (or failure) after a success, and a failure or reversal after an admin FORCED a payout to ``paid`` (Paystack's
  answer was not final, ``payout_attempts.forced``).

A contradiction FREEZES the caregiver (``caregiver_wallets.payouts_frozen_at``) and sets the payouts involved to
``needs_attention``. The only things the code then does with money are: cancel a newer payout that was not sent and let its
reservation pay the take-back (the money is neither lost nor created), debit what the balance holds up to the amount owed, and
record the rest as shortfall (``payouts.shortfall_kobo``). It never credits the balance. While the caregiver is frozen the
batch leaves them out, the send job sends nothing for them, and every later event on ANY of their payouts records the attempt's
outcome and the status but moves no money: a failed or reversed payout becomes ``needs_attention`` and keeps its reservation,
a success just settles it (the reservation becomes the payment, so the balance does not move). Everything that gives money
back is an admin action: ``resolve_payout`` as ``failed`` returns the reservation net of the outstanding shortfall, and
``unfreeze_caregiver`` (refused while a payout still needs attention or is processing) lets the caregiver be paid again. Earnings
keep accruing while a caregiver is frozen, so it first DEBITS the outstanding shortfall from the available balance (up to what
is there: a debit only lowers the balance); a shortfall that is still left refuses the unfreeze unless the admin writes it off,
on purpose, and the audit row records it.

Paystack is never called under a lock, so every admin action that asks Paystack (``retry_payout``, ``resolve_payout``) reads
what its answer depends on AGAIN under the locks (the attempts, the freeze, eligibility) and refuses with a 409 when a webhook
changed them in the meantime.

Lock order, the same as the rest of the money code (booking, transaction rows, wallet): payout batch, then payout rows, then
their ledger rows, then wallets, and wallets in sorted caregiver order when more than one is taken. Paystack is never called
while a lock is held.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.caregiver import (
    TX_CANCELLED,
    TX_FAILED,
    TX_PAYOUT,
    TX_PENDING,
    TX_PENDING_APPROVAL,
    TX_SUCCESSFUL,
    Caregiver,
    CaregiverBankAccount,
    CaregiverTransaction,
    CaregiverWallet,
)
from app.db.models.payout import (
    ATTEMPT_FAILED,
    ATTEMPT_PENDING,
    ATTEMPT_REFUSED,
    ATTEMPT_REVERSED,
    ATTEMPT_SUCCESS,
    BATCH_APPROVED,
    BATCH_CANCELLED,
    BATCH_PENDING_APPROVAL,
    PAYOUT_CANCELLED,
    PAYOUT_FAILED,
    PAYOUT_LIVE,
    PAYOUT_NEEDS_ATTENTION,
    PAYOUT_OPEN,
    PAYOUT_PENDING_APPROVAL,
    PAYOUT_PROCESSING,
    PAYOUT_REVERSED,
    PAYOUT_SUCCESS,
    Payout,
    PayoutAttempt,
    PayoutBatch,
)
from app.db.models.user import User
from app.services import booking_policy, earnings
from app.services.audit import AuditService
from app.services.bank_accounts import is_cooling_down
from app.services.paystack import PaystackService

logger = logging.getLogger(__name__)

DISPATCH_BATCH = 20            # transfers sent per run of the send job
DISPATCH_BUDGET_SECONDS = 35   # stop starting new ones after this long (the worker kills a job at 55 seconds)
RECONCILE_AFTER = timedelta(minutes=15)       # ask Paystack about a transfer that has had no webhook this long
NOT_FOUND_GRACE = timedelta(minutes=30)       # Paystack still has no such transfer this long after we sent it: flag it
RECONCILE_BATCH = 50
OTP_MESSAGE = ("Paystack is waiting for an OTP to release this transfer. Turn off OTP for transfers made through the API "
               "in the Paystack dashboard, then resolve this payout with Paystack.")
NO_RECORD_REASON = "Paystack has no record; check before releasing"
SENDABLE_PROFILE_STATUSES = ("approved", "active")
_RESERVED = (TX_PENDING_APPROVAL, TX_PENDING)  # ledger states in which a payout's money is held out of the balance
# Paystack's transfer statuses
_SUCCESS, _FAILURES, _REVERSED = {"success"}, {"failed", "rejected", "blocked", "abandoned"}, {"reversed"}


class PayoutNotFound(Exception):
    pass


def _naira(kobo: int) -> str:
    return f"\u20a6{kobo // 100:,}" if kobo % 100 == 0 else f"\u20a6{kobo / 100:,.2f}"


class PayoutConflict(Exception):
    """The payout or batch is not in a state that allows this; ``str(exc)`` says why and is safe to show an admin."""


# --- when -------------------------------------------------------------------------------------------------------


def is_payout_day(cadence: str, day: date) -> bool:
    if cadence == "weekly":
        return day.weekday() == 0
    if cadence == "biweekly":
        return day.weekday() == 0 and day.isocalendar()[1] % 2 == 0
    if cadence == "monthly":
        return day.day == 1
    return False


def next_payout_date(cadence: str, today: date) -> Optional[date]:
    """The next payout day strictly after ``today`` (a batch for today, if any, has already been made)."""
    for offset in range(1, 40):
        day = today + timedelta(days=offset)
        if is_payout_day(cadence, day):
            return day
    return None


# --- creating the batch -----------------------------------------------------------------------------------------


def _eligibility_problem(db: Session, caregiver_id: uuid.UUID, now: datetime, *,
                         snapshot: Optional[Payout] = None) -> Optional[str]:
    """Why this caregiver cannot be paid right now (a sentence for an admin), else None. With ``snapshot`` it also checks
    that the bank account is still the one the payout was made for."""
    caregiver = db.query(Caregiver).filter(Caregiver.id == caregiver_id).populate_existing().first()
    if caregiver is None:
        return "the caregiver no longer exists"
    user = db.query(User).filter(User.id == caregiver.user_id).populate_existing().first()
    if caregiver.profile_status == "suspended" or user is None or not user.is_active:
        return "the caregiver is suspended or the account is closed"
    account = (db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver_id)
               .populate_existing().first())
    if db.query(CaregiverWallet.payouts_frozen_at).filter(CaregiverWallet.caregiver_id == caregiver_id).scalar() is not None:
        return "the caregiver's payouts are frozen until an admin has looked at a payout Paystack contradicted"
    if account is None or not account.recipient_code or not account.bank_code:
        return "the caregiver has no verified bank account"
    if is_cooling_down(account, now):
        return "the bank account was changed in the last 24 hours"
    if snapshot is not None and (account.recipient_code != snapshot.recipient_code
                                 or account.account_number != snapshot.account_number):
        return "the bank account was changed after the payout was created"
    return None


def _reserve(db: Session, batch: PayoutBatch, caregiver_id: uuid.UUID, min_kobo: int, now: datetime) -> Optional[Payout]:
    """Take a caregiver's whole available balance into a payout. The caller is inside a savepoint. Returns None when the
    caregiver is no longer eligible once the wallet is locked."""
    wallet = earnings.locked_wallet(db, caregiver_id)
    # What a refund take-back could not take earlier is recovered first: whatever reaches the balance (a returned payout, a
    # release before this feature) is paid out only net of the debt. Decrease-only; applied below, once the payout is certain.
    owed_taken = min(int(wallet.balance or 0), int(wallet.owed_kobo or 0))
    amount = int(wallet.balance or 0) - owed_taken
    if amount < min_kobo or amount <= 0:
        return None
    if _eligibility_problem(db, caregiver_id, now) is not None:  # includes the freeze, read under the wallet lock
        return None
    if db.query(Payout.id).filter(Payout.caregiver_id == caregiver_id, Payout.status.in_(PAYOUT_OPEN)).first():
        return None
    account = db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver_id).one()
    payout_id = uuid.uuid4()
    reference = reference_for(payout_id, 1)
    tx = CaregiverTransaction(
        caregiver_id=caregiver_id, type=TX_PAYOUT, amount=amount, status=TX_PENDING_APPROVAL, bank_name=account.bank_name,
        account_number=account.account_number, account_name=account.account_name, reference=reference,
        narration=f"Payout {batch.period_key}")
    db.add(tx)
    db.flush()
    payout = Payout(
        id=payout_id, batch_id=batch.id, caregiver_id=caregiver_id, transaction_id=tx.id, amount_kobo=amount,
        status=PAYOUT_PENDING_APPROVAL, bank_name=account.bank_name, bank_code=account.bank_code,
        account_number=account.account_number, account_name=account.account_name, recipient_code=account.recipient_code,
        reference=reference, attempts=1)
    db.add(payout)
    db.flush()
    db.add(PayoutAttempt(payout_id=payout_id, attempt=1, reference=reference))
    if owed_taken > 0:
        earnings.recover_owed(db, wallet, owed_taken, why=f"recovered at payout {payout_id}", now=now)
    wallet.balance = int(wallet.balance) - amount - owed_taken  # the reservation: the money leaves the available balance now
    db.flush()  # the unique partial index and the not-negative check speak here
    return payout


def reference_for(payout_id: uuid.UUID, attempt: int) -> str:
    """Paystack wants a lowercase reference of 16 to 50 characters, unique per transfer."""
    return f"po-{payout_id.hex}-{attempt}"


def create_batch(db: Session, *, period_key: str, now: Optional[datetime] = None) -> Optional[PayoutBatch]:
    """Make the batch for ``period_key`` with a payout for every eligible caregiver. One transaction: the batch appears
    whole or not at all. Returns None when the period already has a batch or nobody is due. Commits."""
    now = now or datetime.utcnow()
    policy = booking_policy.current(db)
    min_kobo = max(int(policy.payout_min_naira) * 100, 1)
    if db.query(PayoutBatch.id).filter(PayoutBatch.period_key == period_key).first() is not None:
        return None
    candidates = [row[0] for row in (
        db.query(CaregiverWallet.caregiver_id)
        .join(CaregiverBankAccount, CaregiverBankAccount.caregiver_id == CaregiverWallet.caregiver_id)
        .join(Caregiver, Caregiver.id == CaregiverWallet.caregiver_id)
        .join(User, User.id == Caregiver.user_id)
        .filter(CaregiverWallet.balance >= min_kobo, CaregiverWallet.balance > 0,
                CaregiverBankAccount.recipient_code.isnot(None), CaregiverBankAccount.bank_code.isnot(None),
                CaregiverWallet.payouts_frozen_at.is_(None), Caregiver.profile_status != "suspended", User.is_active.is_(True))
        .order_by(CaregiverWallet.caregiver_id).all())]
    db.rollback()  # end the read transaction; the work below is one new transaction
    if not candidates:
        return None
    try:
        batch = PayoutBatch(period_key=period_key, cadence=policy.payout_cadence, min_payout_kobo=min_kobo,
                            status=BATCH_PENDING_APPROVAL)
        db.add(batch)
        db.flush()
        created = 0
        for caregiver_id in candidates:  # sorted: wallets are always locked in this order
            try:
                with db.begin_nested():
                    if _reserve(db, batch, caregiver_id, min_kobo, now) is not None:
                        created += 1
            except IntegrityError:
                logger.warning("payout for caregiver %s not created (a live payout appeared first)", caregiver_id)
        if not created:
            db.rollback()
            return None
        db.commit()
    except IntegrityError:
        db.rollback()  # another worker made this period's batch first
        return None
    except Exception:
        db.rollback()
        raise
    logger.info("payout batch %s created: %d payout(s)", period_key, created)
    return batch


def create_scheduled_batch(db: Session, now: Optional[datetime] = None) -> Optional[PayoutBatch]:
    """The daily job: on a payout day (per the live policy) make that day's batch."""
    now = now or datetime.utcnow()
    cadence = booking_policy.current(db).payout_cadence
    if not is_payout_day(cadence, now.date()):
        return None
    return create_batch(db, period_key=now.date().isoformat(), now=now)


# --- admin: approve, cancel, retry ---------------------------------------------------------------------------------


def _snapshot(p: Payout) -> Dict[str, Any]:
    return {"status": p.status, "amount_kobo": p.amount_kobo, "caregiver_id": p.caregiver_id, "reference": p.reference,
            "attempts": p.attempts, "shortfall_kobo": p.shortfall_kobo, "returned_kobo": p.returned_kobo,
            "netted_kobo": p.netted_kobo}


def _lock_batch(db: Session, batch_id: uuid.UUID) -> PayoutBatch:
    batch = db.query(PayoutBatch).filter(PayoutBatch.id == batch_id).with_for_update().populate_existing().first()
    if batch is None:
        db.rollback()
        raise PayoutNotFound()
    return batch


def _lock_tx(db: Session, tx_id: uuid.UUID) -> CaregiverTransaction:
    return (db.query(CaregiverTransaction).filter(CaregiverTransaction.id == tx_id)
            .with_for_update().populate_existing().one())


def _return_money(db: Session, caregiver_id: uuid.UUID, amount: int) -> None:
    wallet = earnings.locked_wallet(db, caregiver_id)
    wallet.balance = int(wallet.balance or 0) + int(amount)


def _cancel_locked(db: Session, payout: Payout, tx: CaregiverTransaction, *, admin_id: Optional[uuid.UUID], reason: str,
                   now: datetime) -> None:
    payout.status, tx.status = PAYOUT_CANCELLED, TX_CANCELLED
    payout.cancelled_by, payout.cancelled_at, payout.cancel_reason = admin_id, now, reason
    _return_money(db, payout.caregiver_id, payout.amount_kobo)


def approve_batch(db: Session, batch_id: uuid.UUID, *, admin_id: uuid.UUID, reason: str, request: Optional[Request] = None,
                  now: Optional[datetime] = None) -> PayoutBatch:
    """Approve a batch: its payouts become ``processing`` and the send job pays them. A payout whose caregiver is no longer
    eligible is cancelled instead and its money returned. Commits."""
    now = now or datetime.utcnow()
    batch = _lock_batch(db, batch_id)
    try:
        if batch.status != BATCH_PENDING_APPROVAL:
            raise PayoutConflict(f"This batch is already {batch.status.replace('_', ' ')}")
        payouts = (db.query(Payout).filter(Payout.batch_id == batch.id, Payout.status == PAYOUT_PENDING_APPROVAL)
                   .order_by(Payout.caregiver_id).with_for_update().populate_existing().all())
        if not payouts:
            raise PayoutConflict("This batch has no payouts waiting for approval")
        txs = {t.id: t for t in (db.query(CaregiverTransaction)
                                 .filter(CaregiverTransaction.id.in_([p.transaction_id for p in payouts]))
                                 .order_by(CaregiverTransaction.id).with_for_update().populate_existing().all())}
        audit = AuditService(db)
        approved: List[str] = []
        cancelled: List[Dict[str, str]] = []
        total = 0
        for payout in payouts:
            problem = _eligibility_problem(db, payout.caregiver_id, now, snapshot=payout)
            if problem is not None:
                before = _snapshot(payout)
                _cancel_locked(db, payout, txs[payout.transaction_id], admin_id=None,
                               reason=f"Not paid when the batch was approved: {problem}", now=now)
                audit.record(admin_user_id=admin_id, action="payout.cancel", entity_type="payout", entity_id=payout.id,
                             before=before, after=_snapshot(payout), reason=payout.cancel_reason, request=request)
                cancelled.append({"payout_id": str(payout.id), "reason": problem})
                continue
            payout.status, txs[payout.transaction_id].status = PAYOUT_PROCESSING, TX_PENDING
            payout.approved_by, payout.approved_at = admin_id, now
            approved.append(str(payout.id))
            total += payout.amount_kobo
        batch.status, batch.approved_by, batch.approved_at, batch.approval_reason = BATCH_APPROVED, admin_id, now, reason
        if not approved:
            batch.status = BATCH_CANCELLED  # nothing left to pay
        audit.record(admin_user_id=admin_id, action="payout_batch.approve", entity_type="payout_batch", entity_id=batch.id,
                     before={"status": BATCH_PENDING_APPROVAL},
                     after={"status": batch.status, "approved_payouts": approved, "approved_total_kobo": total,
                            "cancelled_at_approval": cancelled}, reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info("payout batch %s approved by admin %s: %d payout(s), %d kobo", batch.period_key, admin_id, len(approved), total)
    return batch


def cancel_payout(db: Session, payout_id: uuid.UUID, *, admin_id: uuid.UUID, reason: str, request: Optional[Request] = None,
                  now: Optional[datetime] = None) -> Payout:
    """Cancel a payout that has not been approved: the reserved money returns to the caregiver's available balance. Once it
    has been approved the transfer is on its way and only Paystack's answer can end it. Commits."""
    now = now or datetime.utcnow()
    batch_id = db.query(Payout.batch_id).filter(Payout.id == payout_id).scalar()
    if batch_id is None:
        db.rollback()
        raise PayoutNotFound()
    _lock_batch(db, batch_id)  # the same first lock as approving, so the two never interleave
    try:
        payout = db.query(Payout).filter(Payout.id == payout_id).with_for_update().populate_existing().one()
        if payout.status != PAYOUT_PENDING_APPROVAL:
            raise PayoutConflict("Only a payout that has not been approved can be cancelled"
                                 if payout.status != PAYOUT_CANCELLED else "This payout is already cancelled")
        tx = _lock_tx(db, payout.transaction_id)
        before = _snapshot(payout)
        _cancel_locked(db, payout, tx, admin_id=admin_id, reason=reason, now=now)
        db.flush()
        batch = db.query(PayoutBatch).filter(PayoutBatch.id == batch_id).one()
        waiting = db.query(func.count(Payout.id)).filter(Payout.batch_id == batch_id,
                                                         Payout.status == PAYOUT_PENDING_APPROVAL).scalar()
        if batch.status == BATCH_PENDING_APPROVAL and not waiting:
            batch.status = BATCH_CANCELLED
        AuditService(db).record(admin_user_id=admin_id, action="payout.cancel", entity_type="payout", entity_id=payout.id,
                                before=before, after=_snapshot(payout), reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return payout


def cancel_batch(db: Session, batch_id: uuid.UUID, *, admin_id: uuid.UUID, reason: str, request: Optional[Request] = None,
                 now: Optional[datetime] = None) -> PayoutBatch:
    """Cancel a batch that has not been approved: every payout waiting in it is cancelled and its reserved money returns to
    the caregiver's available balance. Commits."""
    now = now or datetime.utcnow()
    batch = _lock_batch(db, batch_id)
    try:
        if batch.status != BATCH_PENDING_APPROVAL:
            raise PayoutConflict(f"This batch is already {batch.status.replace('_', ' ')}")
        waiting = (db.query(Payout).filter(Payout.batch_id == batch.id, Payout.status == PAYOUT_PENDING_APPROVAL)
                   .order_by(Payout.caregiver_id).with_for_update().populate_existing().all())  # wallets locked in this order
        if not waiting:
            raise PayoutConflict("This batch has no payouts waiting for approval")
        txs = {t.id: t for t in (db.query(CaregiverTransaction)
                                 .filter(CaregiverTransaction.id.in_([p.transaction_id for p in waiting]))
                                 .order_by(CaregiverTransaction.id).with_for_update().populate_existing().all())}
        returned = 0
        for payout in waiting:
            _cancel_locked(db, payout, txs[payout.transaction_id], admin_id=admin_id, reason=reason, now=now)
            returned += payout.amount_kobo
        batch.status = BATCH_CANCELLED
        AuditService(db).record(
            admin_user_id=admin_id, action="payout_batch.cancel", entity_type="payout_batch", entity_id=batch.id,
            before={"status": BATCH_PENDING_APPROVAL},
            after={"status": BATCH_CANCELLED, "cancelled_payouts": [str(p.id) for p in waiting],
                   "returned_total_kobo": returned}, reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info("payout batch %s cancelled by admin %s: %d payout(s), %d kobo returned", batch.period_key, admin_id,
                len(waiting), returned)
    return batch


def _verdict(result: Dict[str, Any]) -> Tuple[str, str]:
    """What Paystack's answer to ``verify_transfer`` means: ``paid``, ``failed`` (failed, rejected, blocked, abandoned or
    reversed: the money is back with Oakly), or one of the unknowns ``pending``, ``otp``, ``not_found``, ``unreachable``. The
    second item is a sentence an admin can read."""
    if result.get("status"):
        status = result.get("transfer_status")
        if status in _SUCCESS:
            return "paid", "Paystack reports this transfer as successful"
        if status in _FAILURES | _REVERSED:
            return "failed", f"Paystack reports this transfer as {status}"
        if status == "otp":
            return "otp", "Paystack is waiting for an OTP to release this transfer"
        return "pending", f"Paystack says this transfer is still pending ({status})"
    if result.get("not_found"):
        return "not_found", "Paystack has no record of this transfer"
    return "unreachable", "We cannot reach Paystack right now to check this transfer"


def retry_payout(db: Session, payout_id: uuid.UUID, *, admin_id: uuid.UUID, reason: str, request: Optional[Request] = None,
                 now: Optional[datetime] = None, paystack: Optional[PaystackService] = None) -> Payout:
    """Send a failed or reversed payout again: the money is reserved again (it must still be in the available balance), the
    payout gets a new attempt with a new Paystack reference and the send job pays it. Before anything changes Paystack is asked
    about the CURRENT attempt, and the retry goes ahead only on a definite failed or reversed answer (or, for a transfer
    Paystack itself refused, on its saying it has no such transfer): a transfer that is pending, waiting for an OTP, unknown or
    unreachable could still be paid, and sending again would pay twice. If Paystack says it was paid after all, the payout is
    settled as such (``needs_attention``, the returned money taken back, the caregiver frozen) and the retry is refused. A frozen
    caregiver's payout is never retried. Commits."""
    now = now or datetime.utcnow()
    paystack = paystack or PaystackService()
    current = (db.query(Payout.status, Payout.reference, Payout.attempts, PayoutAttempt.status)
               .join(PayoutAttempt, PayoutAttempt.payout_id == Payout.id)
               .filter(Payout.id == payout_id, PayoutAttempt.attempt == Payout.attempts).first())
    if current is None:
        db.rollback()
        raise PayoutNotFound()
    status_now, current_ref, current_attempts, attempt_status = current
    if status_now not in (PAYOUT_FAILED, PAYOUT_REVERSED):
        db.rollback()
        raise PayoutConflict("Only a failed or reversed payout can be retried")
    db.rollback()  # no lock is held while Paystack is asked
    result = paystack.verify_transfer(current_ref)
    verdict, said = _verdict(result)
    if verdict == "paid":
        apply_transfer_event(db, current_ref, PAYOUT_SUCCESS, amount_kobo=result.get("amount_kobo"),
                             transfer_code=result.get("transfer_code"), now=now)
        raise PayoutConflict("Paystack reports this transfer as successful, so it was not sent again. The payout now needs "
                             "attention: check it before doing anything else")
    if not (verdict == "failed" or (verdict == "not_found" and attempt_status == ATTEMPT_REFUSED)):
        raise PayoutConflict(f"{said}, so it cannot be retried." + {
            "pending": " Wait for Paystack's answer.", "otp": " Resolve the payout instead.",
            "not_found": " Resolve the payout instead.", "unreachable": " Try again in a moment."}[verdict])
    try:
        payout = db.query(Payout).filter(Payout.id == payout_id).with_for_update().populate_existing().one()
        if payout.status not in (PAYOUT_FAILED, PAYOUT_REVERSED) or payout.reference != current_ref:
            raise PayoutConflict("This payout changed while Paystack was being asked. Look at it again")
        tx = _lock_tx(db, payout.transaction_id)
        wallet = earnings.locked_wallet(db, payout.caregiver_id)
        # Eligibility, the freeze included, is read only now: a contradiction freezes a caregiver under the wallet lock, so
        # until we hold it the answer could still change.
        problem = _eligibility_problem(db, payout.caregiver_id, now, snapshot=payout)
        if problem is not None:
            raise PayoutConflict(f"This payout cannot be retried: {problem}")
        if int(wallet.owed_kobo or 0) > 0:  # a retry would pay the returned money again, ahead of the refund debt
            raise PayoutConflict(f"This caregiver owes {_naira(int(wallet.owed_kobo))} from a refund. Cancel this payout "
                                 "instead; the next payout run will take what is owed first")
        if int(wallet.balance or 0) < payout.amount_kobo:
            raise PayoutConflict("The caregiver's available balance is lower than this payout, so it cannot be sent again")
        before = _snapshot(payout)
        previous = (db.query(PayoutAttempt).filter(PayoutAttempt.payout_id == payout.id, PayoutAttempt.attempt == current_attempts)
                    .with_for_update().one())
        previous.failure_reason = previous.failure_reason or payout.failure_reason  # the history stays on the attempt
        payout.attempts += 1
        payout.reference = tx.reference = reference_for(payout.id, payout.attempts)
        db.add(PayoutAttempt(payout_id=payout.id, attempt=payout.attempts, reference=payout.reference))
        payout.status, tx.status = PAYOUT_PROCESSING, TX_PENDING
        payout.transfer_code = payout.failure_reason = payout.initiated_at = payout.completed_at = None
        payout.approved_by, payout.approved_at = admin_id, now
        wallet.balance = int(wallet.balance) - payout.amount_kobo
        db.flush()  # the unique partial index: a caregiver with another live payout cannot have this one too
        AuditService(db).record(admin_user_id=admin_id, action="payout.retry", entity_type="payout", entity_id=payout.id,
                                before=before, after=_snapshot(payout), reason=reason, request=request)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise PayoutConflict("This caregiver already has a payout in progress")
    except Exception:
        db.rollback()
        raise
    return payout


def resolve_payout(db: Session, payout_id: uuid.UUID, *, outcome: str, reason: str, admin_id: uuid.UUID, force: bool = False,
                   request: Optional[Request] = None, now: Optional[datetime] = None,
                   paystack: Optional[PaystackService] = None) -> Payout:
    """An admin's decision on a payout that Paystack's own word has not settled: one that is ``processing`` (for example
    stuck at OTP) or ``needs_attention``. ``outcome`` is ``paid`` (mark it successful) or ``failed`` (return the reserved
    money). Paystack is asked first, about the attempt that was paid if one was, else the current one, and a definite answer
    that contradicts the requested outcome refuses it, ``force`` or not. When Paystack's answer is not final (pending, OTP,
    no record, unreachable) the outcome is allowed only with ``force``. Money that has already been paid out is never
    returned: ``failed`` is refused for a payout with a paid attempt, or whose money is no longer reserved (the exception is
    a payout flagged ``needs_attention`` after its money was already returned: ``failed`` then just closes it, nothing moves,
    and ``paid`` takes the money back from the balance, the rest being recorded as shortfall). Paystack is asked with no lock
    held, so everything the answer depends on is read AGAIN under the locks: another attempt paid in the meantime refuses
    both outcomes.

    ``failed`` is how money comes back to a caregiver whose payouts are frozen, and it is netted: the caregiver's
    outstanding shortfall (money Paystack paid that the balance could not cover) is kept out of the reservation first, so the
    amount returned is ``max(0, reserved - outstanding_shortfall)`` and the shortfall goes down by what is kept. Both numbers
    are recorded on the payout and on its ledger row. Commits."""
    if outcome not in ("paid", "failed"):
        raise ValueError(f"unknown outcome {outcome!r}")
    now = now or datetime.utcnow()
    paystack = paystack or PaystackService()
    payout = db.query(Payout).filter(Payout.id == payout_id).populate_existing().first()
    if payout is None:
        db.rollback()
        raise PayoutNotFound()
    if payout.status not in (PAYOUT_PROCESSING, PAYOUT_NEEDS_ATTENTION):
        status_name = payout.status.replace("_", " ")
        db.rollback()
        raise PayoutConflict(f"A payout that is {status_name} cannot be resolved here; only a processing payout or one "
                             "that needs attention can be")
    caregiver_id = payout.caregiver_id
    attempts = {a.attempt: a for a in db.query(PayoutAttempt).filter(PayoutAttempt.payout_id == payout_id).populate_existing().all()}
    paid_attempt = next((a for a in attempts.values() if a.status == ATTEMPT_SUCCESS), None)
    target = paid_attempt or attempts[payout.attempts]
    target_id, target_ref, target_status = target.id, target.reference, target.status
    paid_attempt_number = paid_attempt.attempt if paid_attempt is not None else None
    seen_attempts, seen_reference = payout.attempts, payout.reference
    tx_status_seen = db.query(CaregiverTransaction.status).filter(CaregiverTransaction.id == payout.transaction_id).scalar()
    reserved = tx_status_seen in _RESERVED
    closable = payout.status == PAYOUT_NEEDS_ATTENTION and tx_status_seen in (TX_FAILED, TX_CANCELLED)
    db.rollback()
    if outcome == "failed" and paid_attempt_number is not None:
        raise PayoutConflict(f"Paystack has already paid attempt {paid_attempt_number} of this payout, so the money cannot "
                             "be returned. Resolve it as paid")
    if outcome == "failed" and not reserved and not closable:
        raise PayoutConflict("The money of this payout is no longer reserved, so it cannot be returned by resolving it. "
                             "Fix the balance with a ledger adjustment")
    result = paystack.verify_transfer(target_ref)  # no lock is held while Paystack is asked
    verdict, said = _verdict(result)
    if verdict == "unreachable" and paid_attempt_number is not None and outcome == "paid":
        verdict, said = "paid", "Paystack reported this transfer as successful earlier"
    if verdict == "not_found" and target_status == ATTEMPT_REFUSED:
        verdict, said = "failed", "Paystack refused this transfer, so it has no record of it"
    expected = "paid" if outcome == "paid" else "failed"
    if verdict in ("paid", "failed") and verdict != expected:
        raise PayoutConflict(f"{said}, so it cannot be resolved as {outcome}")
    if verdict != expected and not force:
        raise PayoutConflict(f"{said}, so there is no proof for either answer. Resolve it only with force and a reason that "
                             "says how you checked")
    forced = bool(force and verdict != expected)
    try:
        rows = _lock_payouts(db, caregiver_id, payout_id, shortfall=True)
        payout = next(r for r in rows if r.id == payout_id)
        if (payout.status not in (PAYOUT_PROCESSING, PAYOUT_NEEDS_ATTENTION) or payout.attempts != seen_attempts
                or payout.reference != seen_reference):
            raise PayoutConflict("This payout changed while Paystack was being asked. Look at it again")
        # The settle lock order: payout, ALL its attempts (by number), ledger row, wallet. Paystack was asked with no lock
        # held, so a webhook may have settled an attempt since: what the answer means is decided again from the locked rows.
        locked_attempts = (db.query(PayoutAttempt).filter(PayoutAttempt.payout_id == payout_id).order_by(PayoutAttempt.attempt)
                           .with_for_update().populate_existing().all())
        attempt = next(a for a in locked_attempts if a.id == target_id)
        paid_now = [a for a in locked_attempts if a.status == ATTEMPT_SUCCESS]
        if outcome == "failed" and paid_now:
            raise PayoutConflict(f"Paystack paid attempt {paid_now[0].attempt} of this payout while Paystack was being asked, "
                                 "so the money cannot be returned. Look at it again and resolve it as paid")
        if outcome == "paid":
            twice = [a for a in paid_now if a.id != target_id]
            if twice:
                raise PayoutConflict(f"Paystack also paid attempt {twice[0].attempt} of this payout while Paystack was being "
                                     "asked, so this would pay it twice. Look at it again")
            if attempt.status != target_status:
                raise PayoutConflict("This payout's attempt changed while Paystack was being asked. Look at it again")
        tx = _lock_tx(db, payout.transaction_id)
        before = _snapshot(payout)
        payout.completed_at = now
        returned = kept = 0
        if outcome == "paid":
            if tx.status in (TX_FAILED, TX_CANCELLED):
                # Paid, but the money had already gone back to the balance: take back what it holds, record the rest.
                wallet = earnings.locked_wallet(db, caregiver_id)
                taken = min(int(wallet.balance or 0), payout.amount_kobo)
                wallet.balance = int(wallet.balance or 0) - taken
                payout.shortfall_kobo += payout.amount_kobo - taken
                tx.status_reason = (f"Resolved as paid after its money was returned: {taken} kobo taken back from the balance, "
                                    f"{payout.amount_kobo - taken} kobo shortfall. {reason}")
                tx.status_changed_by, tx.status_changed_at = admin_id, now
            payout.status, tx.status, attempt.status, payout.failure_reason = PAYOUT_SUCCESS, TX_SUCCESSFUL, ATTEMPT_SUCCESS, None
            attempt.forced = attempt.forced or forced
        else:
            attempt_status = ATTEMPT_REVERSED if result.get("transfer_status") in _REVERSED else ATTEMPT_FAILED
            if attempt.status == ATTEMPT_PENDING:
                attempt.status = attempt_status
            if tx.status in (TX_FAILED, TX_CANCELLED) and payout.status == PAYOUT_NEEDS_ATTENTION:
                # Flagged after its money was already returned (an amount mismatch): nothing is held, so nothing moves.
                payout.status = PAYOUT_CANCELLED if tx.status == TX_CANCELLED else PAYOUT_FAILED
                tx.status_reason = f"Resolved as failed: its money had already been returned, nothing moved. {reason}"
            else:
                if tx.status not in _RESERVED:
                    raise PayoutConflict("The money of this payout is no longer reserved, so it cannot be returned")
                wallet = earnings.locked_wallet(db, caregiver_id)
                # Net the reservation against what the caregiver already has from Paystack beyond their earnings. Only while
                # the caregiver is frozen (unfreezing settles the shortfall first, see ``unfreeze_caregiver``).
                holders = sorted((r for r in rows if r.shortfall_kobo > 0), key=lambda r: (r.created_at, r.id))
                outstanding = sum(r.shortfall_kobo for r in holders) if wallet.payouts_frozen_at is not None else 0
                kept = min(payout.amount_kobo, outstanding)
                returned = payout.amount_kobo - kept
                left = kept
                for holder in holders if kept else []:
                    step = min(holder.shortfall_kobo, left)
                    holder.shortfall_kobo -= step
                    left -= step
                payout.status, tx.status = PAYOUT_FAILED, TX_FAILED
                payout.returned_kobo, payout.netted_kobo = returned, kept
                wallet.balance = int(wallet.balance or 0) + returned
                tx.status_reason = (f"Resolved as failed: {returned} kobo returned to the balance, {kept} kobo kept against a "
                                    f"shortfall. {reason}")
            payout.failure_reason = attempt.failure_reason = f"Resolved as failed by an admin: {reason}"
            tx.status_changed_by, tx.status_changed_at = admin_id, now
        after = {**_snapshot(payout), "outcome": outcome, "forced": forced, "paystack_said": said}
        AuditService(db).record(admin_user_id=admin_id, action="payout.resolve", entity_type="payout", entity_id=payout.id,
                                before=before, after=after, reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.warning("payout %s resolved as %s by admin %s (%s%s)", seen_reference, outcome, admin_id, said,
                   ", forced" if forced else "")
    return payout


def unfreeze_caregiver(db: Session, caregiver_id: uuid.UUID, *, admin_id: uuid.UUID, reason: str,
                       write_off_shortfall: bool = False, request: Optional[Request] = None,
                       now: Optional[datetime] = None) -> Dict[str, Any]:
    """Let a caregiver be paid again. Refused while any of their payouts still needs attention or is processing: Paystack's
    answer on those has not been settled by a person.

    Earnings keep accruing while a caregiver is frozen, so the outstanding shortfall (money Paystack paid beyond what the
    balance could cover) is settled first: it is DEBITED from the available balance, up to what the balance holds (this only
    lowers the balance), the shortfall going down by that amount. What is still owed after that is refused with a 409 unless
    ``write_off_shortfall`` is true, which writes it off and puts the amount in the audit row. A refusal changes nothing.
    Commits."""
    now = now or datetime.utcnow()
    if db.get(Caregiver, caregiver_id) is None:
        db.rollback()
        raise PayoutNotFound()
    try:
        rows = _lock_payouts(db, caregiver_id, shortfall=True, attention=True)
        holders = sorted((r for r in rows if r.shortfall_kobo > 0), key=lambda r: (r.created_at, r.id))
        txs = _lock_txs(db, [r.transaction_id for r in holders])
        wallet = earnings.locked_wallet(db, caregiver_id)
        if wallet.payouts_frozen_at is None:
            raise PayoutConflict("This caregiver's payouts are not frozen")
        open_ones = [r for r in rows if r.status in (PAYOUT_NEEDS_ATTENTION, PAYOUT_PROCESSING)]
        if open_ones:
            raise PayoutConflict(f"{len(open_ones)} payout(s) of this caregiver still need attention or are processing "
                                 f"({', '.join(str(r.id) for r in open_ones)}). Resolve them first")
        outstanding = sum(r.shortfall_kobo for r in holders)
        debit = min(int(wallet.balance or 0), outstanding)
        left_owed = outstanding - debit
        if left_owed and not write_off_shortfall:
            raise PayoutConflict(f"This caregiver still owes {left_owed} kobo that Paystack paid beyond their earnings, even "
                                 f"after {debit} kobo is taken from their available balance. Unfreeze with "
                                 "write_off_shortfall to write it off, or leave them frozen")
        before = {"payouts_frozen_at": wallet.payouts_frozen_at.isoformat(), "payouts_frozen_reason": wallet.payouts_frozen_reason,
                  "outstanding_shortfall_kobo": outstanding}
        to_debit, written_off = debit, left_owed
        per_payout: Dict[str, Dict[str, int]] = {}
        for holder in holders:
            step = min(holder.shortfall_kobo, to_debit)
            to_debit -= step
            dropped = holder.shortfall_kobo - step if write_off_shortfall else 0
            holder.shortfall_kobo -= step + dropped
            per_payout[str(holder.id)] = {"debited_kobo": step, "written_off_kobo": dropped}
            note = (f"Unfreeze: {step} kobo of this payout's shortfall was debited from the available balance"
                    + (f", {dropped} kobo written off" if dropped else "") + f". {reason}")
            tx = txs[holder.transaction_id]
            tx.status_reason = f"{tx.status_reason} | {note}" if tx.status_reason else note
            tx.status_changed_by, tx.status_changed_at = admin_id, now
        wallet.balance = int(wallet.balance or 0) - debit
        wallet.payouts_frozen_at = wallet.payouts_frozen_reason = None
        remaining = outstanding - debit - written_off  # always 0 here: a shortfall that is left is refused or written off
        AuditService(db).record(admin_user_id=admin_id, action="payout.unfreeze", entity_type="caregiver",
                                entity_id=caregiver_id, before=before,
                                after={"payouts_frozen_at": None, "debited_from_balance_kobo": debit,
                                       "written_off_shortfall_kobo": written_off, "outstanding_shortfall_kobo": remaining,
                                       "per_payout": per_payout}, reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.warning("caregiver %s unfrozen by admin %s (shortfall debited from the balance: %d, written off: %d)", caregiver_id,
                   admin_id, debit, written_off)
    return {"caregiver_id": caregiver_id, "payouts_frozen_at": None, "debited_from_balance_kobo": debit,
            "written_off_shortfall_kobo": written_off, "outstanding_shortfall_kobo": remaining}


# --- sending ---------------------------------------------------------------------------------------------------------


def _sendable_problem(db: Session, caregiver_id: uuid.UUID) -> Optional[str]:
    """Checked when the send job claims a payout (the batch was approved some time ago): the caregiver must still be approved
    or active, have an open account and not have been erased."""
    caregiver = db.query(Caregiver).filter(Caregiver.id == caregiver_id).populate_existing().first()
    if caregiver is None:
        return "the caregiver no longer exists"
    user = db.query(User).filter(User.id == caregiver.user_id).populate_existing().first()
    if caregiver.suspension_reason == "account_erased":
        return "the account was erased"
    if user is None or not user.is_active:
        return "the account is closed"
    if caregiver.profile_status not in SENDABLE_PROFILE_STATUSES:
        return f"the caregiver's profile is {caregiver.profile_status or 'incomplete'}, not approved"
    return None


def dispatch_due(db: Session, *, paystack: Optional[PaystackService] = None, limit: int = DISPATCH_BATCH,
                 now: Optional[datetime] = None) -> int:
    """Send approved payouts to Paystack. A payout is claimed (``initiated_at``) and committed before the call, so two
    workers never send the same one, and the call itself holds no lock. A frozen caregiver's payouts are not picked up (an
    admin settles them). A caregiver who is no longer approved, active and
    present is not paid: the payout is cancelled (by the system) and the money returned. Returns how many were sent."""
    paystack = paystack or PaystackService()
    started = time.monotonic()
    sent = 0
    while sent < limit and time.monotonic() - started < DISPATCH_BUDGET_SECONDS:
        frozen = db.query(CaregiverWallet.caregiver_id).filter(CaregiverWallet.payouts_frozen_at.isnot(None))
        payout = (db.query(Payout).filter(Payout.status == PAYOUT_PROCESSING, Payout.initiated_at.is_(None),
                                          Payout.caregiver_id.notin_(frozen))  # a frozen caregiver is never sent anything
                  .order_by(Payout.approved_at, Payout.id).with_for_update(skip_locked=True).populate_existing().first())
        if payout is None:
            db.rollback()
            break
        problem = _sendable_problem(db, payout.caregiver_id)
        if problem is not None:
            try:
                tx = _lock_tx(db, payout.transaction_id)
                _cancel_locked(db, payout, tx, admin_id=None, reason=f"Cancelled before sending: {problem}",
                               now=now or datetime.utcnow())
                db.commit()
            except Exception:
                db.rollback()
                raise
            logger.warning("payout %s cancelled before sending: %s", payout.reference, problem)
            continue
        payout_id, amount, recipient, reference = payout.id, payout.amount_kobo, payout.recipient_code, payout.reference
        payout.initiated_at = now or datetime.utcnow()
        db.commit()
        result = paystack.initiate_transfer(amount_kobo=amount, recipient_code=recipient, reference=reference,
                                            reason="Oakly caregiver payout")
        _record_initiation(db, payout_id, reference, result, now)
        sent += 1
    return sent


def _record_initiation(db: Session, payout_id: uuid.UUID, reference: str, result: Dict[str, Any],
                       now: Optional[datetime]) -> None:
    if result.get("status"):
        status = result.get("transfer_status")
        if status == "otp":
            logger.error("payout %s: Paystack needs an OTP for this transfer; OTP must be off for API transfers", reference)
            _note(db, payout_id, reference, result.get("transfer_code"), OTP_MESSAGE)
        elif status in _SUCCESS:
            apply_transfer_event(db, reference, PAYOUT_SUCCESS, transfer_code=result.get("transfer_code"), now=now)
        elif status in _FAILURES | _REVERSED:
            apply_transfer_event(db, reference, PAYOUT_FAILED, transfer_code=result.get("transfer_code"),
                                 reason=f"Paystack: {status}", now=now)
        else:
            _note(db, payout_id, reference, result.get("transfer_code"), None)
        return
    if result.get("ambiguous"):
        # Paystack may have the transfer: do not return the money. The webhook, or the reconcile job, settles it.
        logger.warning("payout %s: no clear answer from Paystack (%s); left processing", reference, result.get("message"))
        return
    logger.warning("payout %s refused by Paystack: %s", reference, result.get("message"))
    apply_transfer_event(db, reference, PAYOUT_FAILED, reason=f"Paystack refused the transfer: {result.get('message')}",
                         now=now, refused=True)


def _note(db: Session, payout_id: uuid.UUID, reference: str, transfer_code: Optional[str], note: Optional[str]) -> None:
    payout = (db.query(Payout).filter(Payout.id == payout_id, Payout.reference == reference)
              .with_for_update().populate_existing().first())
    if payout is not None and payout.status == PAYOUT_PROCESSING:
        payout.transfer_code = transfer_code or payout.transfer_code
        payout.failure_reason = note
        db.commit()
    else:
        db.rollback()


# --- settling --------------------------------------------------------------------------------------------------------


@dataclass
class _Locked:
    """Everything one event needs, locked in lock order: the payout the reference belongs to and the caregiver's other live
    payouts (in id order), the attempts, the ledger rows, then the wallet."""
    payout: Payout
    attempt: PayoutAttempt
    attempts: List[PayoutAttempt]
    tx: CaregiverTransaction
    others: List[Tuple[Payout, CaregiverTransaction]]
    wallet: CaregiverWallet
    now: datetime

    @property
    def frozen(self) -> bool:
        return self.wallet.payouts_frozen_at is not None


def _lock_payouts(db: Session, caregiver_id: uuid.UUID, payout_id: Optional[uuid.UUID] = None, *, shortfall: bool = False,
                  attention: bool = False) -> List[Payout]:
    """Lock, in id order, the payout and the caregiver's live payouts (and, on request, those that need attention or carry a
    shortfall). One order for everybody: two events on two payouts of the same caregiver never wait on each other crosswise."""
    condition = Payout.status.in_(PAYOUT_LIVE)
    if payout_id is not None:
        condition = or_(condition, Payout.id == payout_id)
    if shortfall:
        condition = or_(condition, Payout.shortfall_kobo > 0)
    if attention:
        condition = or_(condition, Payout.status == PAYOUT_NEEDS_ATTENTION)
    return (db.query(Payout).filter(Payout.caregiver_id == caregiver_id, condition).order_by(Payout.id)
            .with_for_update().populate_existing().all())


def _lock_txs(db: Session, tx_ids: List[uuid.UUID]) -> Dict[uuid.UUID, CaregiverTransaction]:
    if not tx_ids:
        return {}
    return {t.id: t for t in (db.query(CaregiverTransaction).filter(CaregiverTransaction.id.in_(tx_ids))
                              .order_by(CaregiverTransaction.id).with_for_update().populate_existing().all())}


def _locate(db: Session, reference: str, now: datetime) -> Optional[_Locked]:
    """Lock what an event on this attempt reference may touch. None when the reference is not ours."""
    found = (db.query(PayoutAttempt.payout_id, Payout.caregiver_id).join(Payout, Payout.id == PayoutAttempt.payout_id)
             .filter(PayoutAttempt.reference == reference).first())
    if found is None:
        return None
    payout_id, caregiver_id = found
    rows = _lock_payouts(db, caregiver_id, payout_id)
    attempts = (db.query(PayoutAttempt).filter(PayoutAttempt.payout_id == payout_id).order_by(PayoutAttempt.attempt)
                .with_for_update().populate_existing().all())
    txs = _lock_txs(db, [r.transaction_id for r in rows])
    wallet = earnings.locked_wallet(db, caregiver_id)
    # A batch may have reserved a new payout between the read above and the wallet lock (it takes the wallet lock first), so
    # look again now that nothing can change under us.
    held = {r.id for r in rows}
    late = [r for r in (db.query(Payout).filter(Payout.caregiver_id == caregiver_id, Payout.status.in_(PAYOUT_LIVE))
                        .order_by(Payout.id).with_for_update().populate_existing().all()) if r.id not in held]
    txs.update(_lock_txs(db, [r.transaction_id for r in late]))
    rows += late
    payout = next(r for r in rows if r.id == payout_id)
    others = [(r, txs[r.transaction_id]) for r in rows if r.id != payout_id and r.status in PAYOUT_LIVE]
    return _Locked(payout, next(a for a in attempts if a.reference == reference), attempts, txs[payout.transaction_id],
                   others, wallet, now)


def _freeze(ctx: _Locked, reason: str) -> None:
    """Stop paying this caregiver until an admin looks. The first reason stays: it is what started the freeze."""
    if ctx.wallet.payouts_frozen_at is None:
        ctx.wallet.payouts_frozen_at, ctx.wallet.payouts_frozen_reason = ctx.now, reason
        logger.error("caregiver %s: payouts frozen: %s", ctx.payout.caregiver_id, reason)


def _flag(payout: Payout, note: str) -> None:
    payout.status = PAYOUT_NEEDS_ATTENTION
    if note not in (payout.failure_reason or ""):
        payout.failure_reason = f"{payout.failure_reason} {note}".strip() if payout.failure_reason else note


def _take_back(ctx: _Locked, owed: int, why: str) -> Tuple[int, int]:
    """Paystack paid ``owed`` kobo that the caregiver's balance (or a later reservation) may also still hold. This only ever
    lowers what the caregiver can be paid: a newer payout that has not been sent is cancelled and its reservation joins the
    balance as the pool the take-back is paid from, a newer payout that was sent cannot be recalled and is flagged, then the
    pool is debited up to ``owed``. What it cannot cover is recorded as shortfall. Nothing is credited. Returns
    ``(taken, shortfall)``."""
    owed = max(0, int(owed))
    pool = int(ctx.wallet.balance or 0)
    for newer, newer_tx in ctx.others:
        if newer.status not in PAYOUT_LIVE:
            continue
        if newer.status == PAYOUT_PENDING_APPROVAL or newer.initiated_at is None:
            newer.status, newer_tx.status = PAYOUT_CANCELLED, TX_CANCELLED
            newer.cancelled_by, newer.cancelled_at = None, ctx.now
            newer.cancel_reason = f"superseded: earlier payout {ctx.payout.id} confirmed paid by Paystack ({why})"
            pool += newer.amount_kobo  # its reservation pays the take-back, so the money is neither lost nor created
            logger.warning("payout %s cancelled: earlier payout %s was confirmed paid by Paystack", newer.reference, ctx.payout.id)
        else:
            _flag(newer, f"Payout {ctx.payout.id} was confirmed paid by Paystack after this one was sent. Check Paystack for "
                         "both before resolving this payout.")
    taken = min(pool, owed)
    ctx.wallet.balance = pool - taken
    return taken, owed - taken


def apply_transfer_event(db: Session, reference: str, outcome: str, *, amount_kobo: Optional[int] = None,
                         transfer_code: Optional[str] = None, reason: Optional[str] = None, refused: bool = False,
                         now: Optional[datetime] = None) -> str:
    """Settle a payout from Paystack's word about one ATTEMPT: ``reference`` is the attempt's, ``outcome`` is ``success``,
    ``failed`` or ``reversed``. Safe to call any number of times for the same event (a webhook retry, or the webhook and the
    reconcile job together): each transition happens from one state only. Returns ``applied``, ``duplicate``,
    ``unknown_reference``, ``amount_mismatch`` or ``needs_attention`` (Paystack contradicted what we recorded, or the payout
    cannot be settled without a person: the caregiver is frozen, see the module docstring). Commits."""
    now = now or datetime.utcnow()
    try:
        ctx = _locate(db, reference, now)
        if ctx is None:
            db.rollback()
            return "unknown_reference"
        if outcome == PAYOUT_SUCCESS:
            result = _on_success(ctx, amount_kobo)
        elif outcome in (PAYOUT_FAILED, PAYOUT_REVERSED):
            result = _on_failure(ctx, outcome, reason, refused)
        else:
            raise ValueError(f"unknown outcome {outcome!r}")
        if transfer_code and ctx.attempt.attempt == ctx.payout.attempts and not ctx.payout.transfer_code:
            ctx.payout.transfer_code = transfer_code
        if result == "applied":
            logger.info("payout %s settled as %s (%s kobo)", reference, outcome, ctx.payout.amount_kobo)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _on_success(ctx: _Locked, amount_kobo: Optional[int]) -> str:
    payout, attempt, tx = ctx.payout, ctx.attempt, ctx.tx
    if attempt.status == ATTEMPT_SUCCESS:
        return "duplicate"
    is_current = attempt.attempt == payout.attempts
    if amount_kobo is not None and int(amount_kobo) != payout.amount_kobo:
        # We cannot tell what was paid, so nothing moves: the money stays as it is and the payout is never sent again.
        note = (f"Paystack reported success for {amount_kobo} kobo but this payout is {payout.amount_kobo} kobo. Check "
                "Paystack before resolving this payout.")
        _freeze(ctx, f"Payout {payout.id}: Paystack reported a different amount than the payout's")
        _flag(payout, note)  # whatever its status: the unfreeze guard must see a payout that has to be looked at
        logger.error("payout %s: %s", attempt.reference, note)
        return "amount_mismatch"
    paid_before = any(a.status == ATTEMPT_SUCCESS for a in ctx.attempts if a is not attempt)
    status = payout.status
    if is_current and not paid_before:
        if status == PAYOUT_SUCCESS:
            attempt.status = ATTEMPT_SUCCESS  # an admin had already resolved it as paid
            return "duplicate"
        if status in (PAYOUT_PROCESSING, PAYOUT_NEEDS_ATTENTION) and tx.status == TX_PENDING:
            # The ordinary case, and a frozen caregiver's reserved payout: the reservation becomes the payment, so the
            # balance does not move.
            attempt.status = ATTEMPT_SUCCESS
            payout.status, tx.status, payout.completed_at, payout.failure_reason = PAYOUT_SUCCESS, TX_SUCCESSFUL, ctx.now, None
            return "applied"
    attempt.status = ATTEMPT_SUCCESS  # recorded; an earlier failure reason stays on the attempt
    returned = tx.status in (TX_FAILED, TX_CANCELLED)
    if returned or tx.status == TX_SUCCESSFUL or paid_before:
        # Paid, but the money was already returned, or already paid once: take back what the balance holds.
        when = f"after the payout was marked {status}" if returned else "as well as an earlier payment"
        taken, shortfall = _take_back(ctx, payout.amount_kobo, f"attempt {attempt.attempt} was paid {when}")
        payout.shortfall_kobo += shortfall
        tx.status, payout.completed_at = TX_SUCCESSFUL, payout.completed_at or ctx.now
        note = (f"Paystack paid attempt {attempt.attempt} {when}; {taken} kobo was taken back from the available balance"
                + (f", {shortfall} kobo could not be recovered (shortfall)." if shortfall else "."))
    else:
        # The money is still reserved but this is not the payment it was reserved for (an earlier attempt, or a payout that
        # was never sent): the reservation stands and the payout is never sent. One payment, one reservation.
        note = (f"Paystack paid attempt {attempt.attempt}, but attempt {payout.attempts} is the current one and its money is "
                "still reserved. Check Paystack for both before resolving this payout." if not is_current
                else f"Paystack reported attempt {attempt.attempt} as paid while this payout was {status}. Check Paystack "
                     "before resolving this payout.")
    _freeze(ctx, f"Payout {payout.id}: {note}")
    _flag(payout, note)
    return "needs_attention"


def _on_failure(ctx: _Locked, outcome: str, reason: Optional[str], refused: bool) -> str:
    payout, attempt, tx = ctx.payout, ctx.attempt, ctx.tx
    if attempt.status in (ATTEMPT_FAILED, ATTEMPT_REFUSED, ATTEMPT_REVERSED) or (
            outcome == PAYOUT_FAILED and attempt.status == ATTEMPT_SUCCESS and not attempt.forced):
        # (A ``failed`` after a success Paystack itself reported is a stale event. After a success an ADMIN forced it is not:
        # Paystack never said it was paid, so its failure contradicts the resolution.)
        return "duplicate"
    was_paid = attempt.status == ATTEMPT_SUCCESS
    message = reason or ("Paystack reversed the transfer" if outcome == PAYOUT_REVERSED else "Paystack could not complete the transfer")
    attempt.status = ATTEMPT_REFUSED if refused else (ATTEMPT_REVERSED if outcome == PAYOUT_REVERSED else ATTEMPT_FAILED)
    attempt.failure_reason = message
    status = payout.status
    if was_paid or (status == PAYOUT_SUCCESS and attempt.attempt == payout.attempts):
        # A reversal after a success (or a failure for a payout we settled as paid): Paystack's two answers disagree. Do not
        # give the money back: it stays accounted for (pending) until an admin resolves it.
        note = (f"Paystack reported attempt {attempt.attempt} as {outcome} after it had been paid. Check Paystack, then resolve "
                "this payout as failed (the money is returned) or paid.")
        _freeze(ctx, f"Payout {payout.id}: {note}")
        _flag(payout, note)
        if tx.status == TX_SUCCESSFUL:
            tx.status = TX_PENDING
        return "needs_attention"
    if attempt.attempt != payout.attempts:
        return "applied"  # an earlier attempt: it is on record, the live attempt is unaffected
    if status == PAYOUT_PROCESSING and not ctx.frozen:
        payout.status, tx.status, payout.completed_at, payout.failure_reason = outcome, TX_FAILED, ctx.now, message
        ctx.wallet.balance = int(ctx.wallet.balance or 0) + payout.amount_kobo
        return "applied"
    if status in (PAYOUT_PROCESSING, PAYOUT_NEEDS_ATTENTION):
        # Frozen: Paystack's word cannot be trusted to be the last, so no money returns. The attempt and status are recorded.
        _flag(payout, f"Later, Paystack reported attempt {attempt.attempt} as {outcome}; its money stays reserved until an "
                      "admin resolves this payout.")
        logger.error("payout %s: %s while the caregiver's payouts are frozen; resolve by hand", attempt.reference, outcome)
        return "needs_attention"
    return "duplicate"


def _flag_no_record(db: Session, reference: str) -> bool:
    """Reconcile found Paystack has no record of a transfer we sent: keep the money reserved, freeze, and ask for a person."""
    try:
        ctx = _locate(db, reference, datetime.utcnow())
        if ctx is None or ctx.payout.status != PAYOUT_PROCESSING or ctx.attempt.attempt != ctx.payout.attempts:
            db.rollback()
            return False
        ctx.payout.status, ctx.payout.failure_reason = PAYOUT_NEEDS_ATTENTION, NO_RECORD_REASON
        _freeze(ctx, f"Payout {ctx.payout.id}: {NO_RECORD_REASON}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.error("payout %s: %s", reference, NO_RECORD_REASON)
    return True


def reconcile(db: Session, *, paystack: Optional[PaystackService] = None, now: Optional[datetime] = None,
              limit: int = RECONCILE_BATCH) -> int:
    """Ask Paystack about payouts that were sent a while ago and have no answer yet (a webhook that never arrived). A
    definite answer settles the payout; Paystack having no record of it does NOT return the money (the transfer may exist
    and succeed later): the payout becomes ``needs_attention`` with the money still reserved. Returns how many were acted on."""
    paystack = paystack or PaystackService()
    now = now or datetime.utcnow()
    due = [(p.reference, p.initiated_at, p.transfer_code) for p in (
        db.query(Payout).filter(Payout.status == PAYOUT_PROCESSING, Payout.initiated_at.isnot(None),
                                Payout.initiated_at <= now - RECONCILE_AFTER)
        .order_by(Payout.initiated_at).limit(limit).all())]
    db.rollback()
    settled = 0
    for reference, initiated_at, transfer_code in due:
        result = paystack.verify_transfer(reference)
        outcome: Optional[str] = None
        reason: Optional[str] = None
        if result.get("status"):
            status = result.get("transfer_status")
            if status in _SUCCESS:
                outcome = PAYOUT_SUCCESS
            elif status in _FAILURES:
                outcome, reason = PAYOUT_FAILED, f"Paystack: {status}"
            elif status in _REVERSED:
                outcome, reason = PAYOUT_REVERSED, "Paystack reversed the transfer"
        elif result.get("not_found") and now - initiated_at >= NOT_FOUND_GRACE:
            if _flag_no_record(db, reference):
                settled += 1
            continue
        if outcome is None:
            continue
        if apply_transfer_event(db, reference, outcome, amount_kobo=result.get("amount_kobo") if outcome == PAYOUT_SUCCESS
                                else None, transfer_code=result.get("transfer_code"), reason=reason, now=now) in (
                "applied", "needs_attention"):
            settled += 1
    return settled


# --- what a caregiver sees ---------------------------------------------------------------------------------------------


def in_progress_kobo(db: Session, caregiver_id: uuid.UUID) -> int:
    """Money reserved for a payout that is waiting for approval or on its way to the bank."""
    return int(db.query(func.coalesce(func.sum(CaregiverTransaction.amount), 0)).filter(
        CaregiverTransaction.caregiver_id == caregiver_id, CaregiverTransaction.type == TX_PAYOUT,
        CaregiverTransaction.status.in_((TX_PENDING_APPROVAL, TX_PENDING))).scalar() or 0)
