"""Refunds v2 (docs/plans/2026-10-21-refunds-v2-design.md): creating refund rows, and what the policy suggests for each.

A refund row is made ``pending_approval`` by a cancel path (``open_system_refund``, called by
``booking_cancellation.flag_refund`` and the late-payment path) or by staff (``request_refund``), approved by a person
(``approve_refund``), then sent to Paystack by ``dispatch_due`` and settled by Paystack's word (``apply_refund_event`` from the
``refund.*`` webhooks, and ``reconcile``). Money is integer kobo; the Paystack fee is never deducted from a suggestion.

The invariant of the send path (mirrors ``payouts``): an attempt is claimed and COMMITTED before Paystack is called, so a crash,
a timeout or a second worker can never send it twice; silence from Paystack leaves the attempt ``unknown`` and the refund
``processing`` (never resent, never "failed"); a contradiction between Paystack and what we recorded puts the refund in
``needs_review`` and nothing else moves by itself. No lock is held across a call to Paystack.

Lock order, as everywhere in the money code: booking, refund, refund attempts, caregiver ledger rows, wallets.
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.admin import AdminUser
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import TX_EARNING, CaregiverTransaction
from app.db.models.patient import Patient
from app.db.models.refund import (
    ATTEMPT_FAILED, ATTEMPT_NEEDS_ATTENTION, ATTEMPT_PENDING, ATTEMPT_PROCESSED, ATTEMPT_PROCESSING, ATTEMPT_SENDING,
    ATTEMPT_UNKNOWN, REFUND_APPROVED, REFUND_CLOSED, REFUND_COMMITTED_STATUSES, REFUND_DEST_ORIGINAL, REFUND_DEST_WALLET,
    REFUND_FAILED, REFUND_NEEDS_ATTENTION, REFUND_NEEDS_REVIEW, REFUND_OPEN_STATUSES, REFUND_ORIGIN_STAFF,
    REFUND_ORIGIN_SYSTEM, REFUND_PENDING_APPROVAL, REFUND_PROCESSING, REFUND_REASON_CODES, REFUND_REJECTED,
    REFUND_SUCCEEDED, Refund, RefundAttempt,
)
from app.db.models.user import User
from app.services import booking_events, booking_policy, earnings, patient_wallet
from app.services.audit import AuditService
from app.services.booking_cancellation import TIER_LATE, TIER_STANDARD, CancelPolicy, _fee
from app.services.paystack import PaystackService

logger = logging.getLogger(__name__)

DISPATCH_BATCH = 20            # refunds sent per run of the send job
DISPATCH_BUDGET_SECONDS = 35   # stop starting new ones after this long (the worker kills a job at 55 seconds)
RECONCILE_AFTER = timedelta(minutes=10)    # ask Paystack about a refund that has had no word this long
NOT_FOUND_GRACE = timedelta(minutes=30)    # Paystack still has no record this long after we sent it: a person looks
RECONCILE_BATCH = 50
NO_RECORD_REASON = ("Paystack has no record of this refund, but it may have been sent. Check the Paystack dashboard before "
                    "resolving it.")
BANK_DETAILS_STATUS_TEXT = "waiting for your bank details. Our team will contact you"

CANCELLED_BY_BOOKER = "cancelled_by_booker"
CAREGIVER_CANCELLED = "caregiver_cancelled"
NO_FAULT_REASONS = ("no_caregiver_found", "paid_after_expiry", "paid_after_cancellation")


class RefundError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RefundNotFound(RefundError):
    """-> 404"""


class RefundConflict(RefundError):
    """-> 409"""


class RefundForbidden(RefundError):
    """-> 403"""


class RefundInvalid(RefundError):
    """-> 422"""


class RefundPaystackUnavailable(RefundError):
    """-> 503"""


def refundable_kobo(db: Session, booking: Booking) -> int:
    """What can still be refunded: the amount paid less every refund already committed (approved or further on)."""
    committed = (db.query(func.coalesce(func.sum(Refund.amount_kobo), 0))
                 .filter(Refund.booking_id == booking.id, Refund.status.in_(REFUND_COMMITTED_STATUSES)).scalar())
    return max(0, int(booking.total_amount_kobo) - int(committed or 0))


def _committed_others_kobo(db: Session, booking: Booking, refund: Refund) -> int:
    """Σ amount of this booking's OTHER refunds that are committed (the cumulative base for the limit, RF-D12)."""
    return int(db.query(func.coalesce(func.sum(Refund.amount_kobo), 0))
               .filter(Refund.booking_id == booking.id, Refund.id != refund.id,
                       Refund.status.in_(REFUND_COMMITTED_STATUSES)).scalar() or 0)


def suggested_amount_kobo(booking: Booking, reason_code: str, entered_kobo: Optional[int]) -> Optional[int]:
    """What the policy says (spec section 5). None means "the approver must enter the amount"."""
    total = int(booking.total_amount_kobo)
    if reason_code == CANCELLED_BY_BOOKER:
        tier = booking.cancel_fee_tier
        policy = CancelPolicy.from_snapshot(booking.cancel_policy)  # the booking's frozen copy, never today's policy
        if tier in (TIER_STANDARD, TIER_LATE) and policy is not None:
            return total - min(_fee(booking, policy.percent(tier)), total)
        if tier in (TIER_STANDARD, TIER_LATE):
            return entered_kobo  # no usable snapshot: the amount the cancel decided
        return total
    if reason_code == CAREGIVER_CANCELLED:
        return None if booking.actual_start_time is not None else total  # a started visit: a person decides
    if reason_code in NO_FAULT_REASONS:
        return total
    # admin_cancelled (full passes the total, partial the admin's amount), complaint, other: what was entered
    if reason_code == "admin_cancelled" and entered_kobo is None:
        return total
    return entered_kobo


def _open_refund(db: Session, booking_id: UUID) -> Optional[Refund]:
    return db.query(Refund).filter(Refund.booking_id == booking_id, Refund.status.in_(REFUND_OPEN_STATUSES)).first()


def open_system_refund(db: Session, booking: Booking, reason_code: str, entered_kobo: Optional[int]) -> Optional[Refund]:
    """Create the pending refund for a cancel path. No commit: it runs in the caller's transaction with the booking locked.
    Returns None (and does nothing) when the booking is not paid or already has an open refund.

    ``booking.refund_required`` is deliberately left set: the family's "refund being processed" WhatsApp notice is
    rendered at send time and is dropped as stale when the flag is clear (booking_notices), as are other readers
    (my_bookings, notification_service). Retiring the flag belongs to the task that settles refunds."""
    if reason_code not in REFUND_REASON_CODES:
        raise ValueError(f"unknown refund reason {reason_code}")
    if (booking.payment_status not in (PaymentStatus.PAID, PaymentStatus.PARTIALLY_REFUNDED)
            or _open_refund(db, booking.id) is not None):
        return None
    suggestion = suggested_amount_kobo(booking, reason_code, entered_kobo)
    if suggestion is not None:
        suggestion = min(suggestion, refundable_kobo(db, booking))  # never suggest more than is left to refund
    refund = Refund(
        booking_id=booking.id, patient_id=booking.patient_id, reason_code=reason_code, origin=REFUND_ORIGIN_SYSTEM,
        suggested_amount_kobo=suggestion,
        status=REFUND_PENDING_APPROVAL)
    db.add(refund)
    db.flush()
    return refund


def request_refund(db: Session, *, booking_id: UUID, amount_kobo: int, reason_code: str, note: Optional[str],
                   admin: AdminUser, request: Optional[Request] = None) -> Refund:
    """Staff raise a refund (a complaint, or anything else). Commits."""
    if reason_code not in REFUND_REASON_CODES:
        db.rollback()
        raise RefundInvalid("Unknown refund reason")
    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().populate_existing().first()
    if booking is None:
        db.rollback()
        raise RefundNotFound("Booking not found")
    if booking.payment_status not in (PaymentStatus.PAID, PaymentStatus.PARTIALLY_REFUNDED):
        db.rollback()
        raise RefundInvalid("This booking has no payment to refund")
    if _open_refund(db, booking.id) is not None:
        db.rollback()
        raise RefundConflict("This booking already has a refund in progress")
    refundable = refundable_kobo(db, booking)
    if amount_kobo <= 0 or amount_kobo > refundable:
        db.rollback()
        raise RefundInvalid(f"The amount must be between 1 and {refundable} kobo, what is left to refund")
    refund = Refund(
        booking_id=booking.id, patient_id=booking.patient_id, reason_code=reason_code, origin=REFUND_ORIGIN_STAFF,
        requested_by_admin_id=admin.id, suggested_amount_kobo=amount_kobo, status=REFUND_PENDING_APPROVAL, note=note)
    db.add(refund)
    db.flush()
    AuditService(db).record(
        admin_user_id=admin.id, action="refund.request", entity_type="refund", entity_id=refund.id,
        after={"booking_id": str(booking.id), "reason_code": reason_code, "amount_kobo": amount_kobo, "note": note},
        request=request)
    db.commit()
    return refund


# --- approve and reject (spec 6.2) ---------------------------------------------------------------------------------
def _naira(kobo: int) -> str:
    return f"\u20a6{kobo // 100:,}" if kobo % 100 == 0 else f"\u20a6{kobo / 100:,.2f}"


def _round_half_up_ratio(amount: int, numerator: int, denominator: int) -> int:
    """round_half_up(amount * numerator / denominator) in integers."""
    return (2 * amount * numerator + denominator) // (2 * denominator)


def take_back_preview_kobo(db: Session, refund: Refund, amount_kobo: int) -> int:
    """What taking the caregiver's share back would be for ``amount_kobo`` (spec 7): the visit earning's share of the
    refund, capped at the earning less earlier take-backs on this booking. 0 when the booking is not completed or has no
    ``earning`` row. Read-only; the approver sees this number before confirming."""
    booking = db.get(Booking, refund.booking_id)
    if booking is None or booking.status != BookingStatus.COMPLETED or int(booking.total_amount_kobo) <= 0:
        return 0
    earning = (db.query(CaregiverTransaction)
               .filter(CaregiverTransaction.booking_id == booking.id, CaregiverTransaction.type == TX_EARNING).first())
    if earning is None:
        return 0
    earlier = int(db.query(func.coalesce(func.sum(Refund.take_back_kobo), 0))
                  .filter(Refund.booking_id == booking.id, Refund.id != refund.id,
                          Refund.status.in_(REFUND_COMMITTED_STATUSES)).scalar() or 0)
    share = _round_half_up_ratio(int(earning.amount), amount_kobo, int(booking.total_amount_kobo))
    return max(0, min(share, int(earning.amount) - earlier))


def _apply_take_back(db: Session, refund: Refund, booking: Booking, now: datetime) -> None:
    """Take the caregiver's share back (wallet first, the rest owed). Runs only from ``_mark_succeeded``."""
    earnings.apply_take_back(db, caregiver_id=booking.caregiver_id, booking_id=booking.id, refund_id=refund.id,
                             amount_kobo=int(refund.take_back_kobo), now=now)


def _mark_succeeded(db: Session, refund: Refund, booking: Booking, now: datetime) -> None:
    """The one success routine (wallet credit now; the Paystack settle paths reuse it). Caller holds the booking and
    refund locks, and must have checked the refund is in a state that may succeed. Does not commit."""
    refund.status = REFUND_SUCCEEDED
    refund.processed_at = now
    db.flush()
    refunded = int(db.query(func.coalesce(func.sum(Refund.amount_kobo), 0))
                   .filter(Refund.booking_id == booking.id, Refund.status == REFUND_SUCCEEDED).scalar() or 0)
    booking.payment_status = (PaymentStatus.REFUNDED if refunded >= int(booking.total_amount_kobo)
                              else PaymentStatus.PARTIALLY_REFUNDED)
    booking.refund_amount_kobo = refund.amount_kobo  # the family's "complete" notice reads the amount from here
    booking.refund_required = False  # the flag was the queue marker; the refund row is the record now
    if int(refund.take_back_kobo or 0) > 0:
        _apply_take_back(db, refund, booking, now)
    # Keyed by the refund, so a second refund on the same booking still tells the family.
    booking_events.emit(db, booking, "refund_processed", f"{refund.id}:complete")


def _lock(db: Session, refund_id: UUID) -> tuple:
    """Booking, then refund (the lock order). Raises RefundNotFound."""
    booking_id = db.query(Refund.booking_id).filter(Refund.id == refund_id).scalar()
    if booking_id is None:
        raise RefundNotFound("Refund not found")
    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().populate_existing().one()
    refund = db.query(Refund).filter(Refund.id == refund_id).with_for_update().populate_existing().one()
    return booking, refund


def _check_requester(refund: Refund, admin: AdminUser) -> None:
    if refund.origin == REFUND_ORIGIN_STAFF and refund.requested_by_admin_id == admin.id:
        raise RefundForbidden("You raised this refund, so someone else must approve it.")


def approve_refund(db: Session, refund_id: UUID, *, admin: AdminUser, amount_kobo: Optional[int],
                   destination: str = REFUND_DEST_ORIGINAL, take_back_from_caregiver: bool = False,
                   take_back_reason: Optional[str] = None, note: Optional[str] = None,
                   request: Optional[Request] = None) -> Refund:
    """Approve a pending refund (spec 6.2). Commits. ``original_method`` leaves it ``approved`` for the send job;
    ``wallet`` credits the patient wallet and succeeds in the same transaction."""
    try:
        refund = _approve(db, refund_id, admin, amount_kobo, destination, take_back_from_caregiver, take_back_reason,
                          note, request)
        db.commit()
        return refund
    except BaseException:
        db.rollback()
        raise


def _approve(db: Session, refund_id: UUID, admin: AdminUser, amount_kobo: Optional[int], destination: str,
             take_back: bool, take_back_reason: Optional[str], note: Optional[str], request: Optional[Request]) -> Refund:
    booking, refund = _lock(db, refund_id)
    if refund.status != REFUND_PENDING_APPROVAL:
        raise RefundConflict("This refund is no longer waiting for approval")
    _check_requester(refund, admin)
    if destination not in (REFUND_DEST_ORIGINAL, REFUND_DEST_WALLET):
        raise RefundInvalid("Unknown refund destination")

    amount = amount_kobo if amount_kobo is not None else refund.suggested_amount_kobo
    if amount is None:
        raise RefundInvalid("Enter the amount to refund")
    refundable = refundable_kobo(db, booking)  # this refund is pending, so it is not in the committed sum
    if amount <= 0 or amount > refundable:
        raise RefundInvalid(f"The amount must be between \u20a60.01 and {_naira(refundable)}, what is left to refund")

    can_above = "refunds.approve_above_limit" in admin.permissions
    limit_kobo = booking_policy.current(db).finance_refund_limit_naira * 100
    cumulative = amount + _committed_others_kobo(db, booking, refund)
    if cumulative > limit_kobo and not can_above:
        raise RefundForbidden(
            f"Refunds on this booking would total {_naira(cumulative)}, above the {_naira(limit_kobo)} limit for Finance. "
            "A Super Admin must approve it.")

    if destination == REFUND_DEST_WALLET:
        if not can_above:
            raise RefundForbidden("Only a Super Admin can refund to the Oakly wallet.")
        # An app booker is their own patient with an account. A WhatsApp (or admin-made) booking has no app wallet owner.
        owner_active = (db.query(User.is_active).join(Patient, Patient.user_id == User.id)
                        .filter(Patient.id == booking.patient_id).scalar()) if booking.channel == "app" else None
        if owner_active is None:
            raise RefundInvalid("Wallet credit is only for app users. Refund this one to the original payment method.")
        if not owner_active:
            raise RefundInvalid("This account is closed, so it cannot be credited. Refund to the original payment method.")

    take_back_kobo = 0
    if take_back:
        if booking.status != BookingStatus.COMPLETED:
            raise RefundInvalid("The caregiver's share can only be taken back for a completed visit")
        if not (take_back_reason or "").strip():
            raise RefundInvalid("Give a reason for taking back the caregiver's share")
        take_back_kobo = take_back_preview_kobo(db, refund, amount)
        if take_back_kobo <= 0:
            raise RefundInvalid("There is no caregiver share left to take back for this booking")

    now = datetime.utcnow()
    before = {"status": refund.status, "suggested_amount_kobo": refund.suggested_amount_kobo}
    refund.amount_kobo = amount
    refund.destination = destination
    refund.approved_by_admin_id = admin.id
    refund.approved_at = now
    refund.take_back_from_caregiver = take_back
    refund.take_back_kobo = take_back_kobo
    refund.take_back_reason = take_back_reason.strip() if take_back else None
    if note:
        refund.note = f"{refund.note}\n{note}" if refund.note else note
    refund.status = REFUND_APPROVED
    db.flush()
    if destination == REFUND_DEST_WALLET:
        # Lock order: the success routine's take-back locks caregiver rows and wallet first; the patient wallet is last.
        _mark_succeeded(db, refund, booking, now)
        patient_wallet.credit_refund_locked(db, booking.patient_id, amount, refund.id)
    AuditService(db).record(
        admin_user_id=admin.id, action="refund.approve", entity_type="refund", entity_id=refund.id, before=before,
        after={"booking_id": str(booking.id), "status": refund.status, "amount_kobo": amount, "destination": destination,
               "take_back_from_caregiver": take_back, "take_back_kobo": take_back_kobo, "limit_kobo": limit_kobo,
               "cumulative_kobo": cumulative, "above_limit": cumulative > limit_kobo},
        reason=note, request=request)
    return refund


def reject_refund(db: Session, refund_id: UUID, *, admin: AdminUser, reason: str,
                  request: Optional[Request] = None) -> Refund:
    """Reject a pending refund; terminal, and it frees the booking's open-refund slot. Commits."""
    try:
        if not (reason or "").strip():
            raise RefundInvalid("Give a reason for rejecting this refund")
        booking, refund = _lock(db, refund_id)
        if refund.status != REFUND_PENDING_APPROVAL:
            raise RefundConflict("This refund is no longer waiting for approval")
        refund.status = REFUND_REJECTED  # the raiser may withdraw their own request: RF-D11 only bars self-approval
        refund.rejected_reason = reason.strip()
        db.flush()
        AuditService(db).record(
            admin_user_id=admin.id, action="refund.reject", entity_type="refund", entity_id=refund.id,
            before={"status": REFUND_PENDING_APPROVAL},
            after={"booking_id": str(booking.id), "status": refund.status, "amount_kobo": refund.suggested_amount_kobo},
            reason=reason.strip(), request=request)
        db.commit()
        return refund
    except BaseException:
        db.rollback()
        raise


# --- sending (spec 6.3) ---------------------------------------------------------------------------------------------
_CLAIM_AGAIN = object()  # a claim that did not lead to a send (state moved on, or the refund could not be sent): look again


def _lock_attempts(db: Session, refund_id: UUID) -> List[RefundAttempt]:
    return (db.query(RefundAttempt).filter(RefundAttempt.refund_id == refund_id).order_by(RefundAttempt.attempt)
            .with_for_update().populate_existing().all())


def _claim_for_send(db: Session, now: datetime):
    """Lock one approved original-method refund (booking first; a booking another worker holds is skipped, never waited
    for), start attempt N as ``sending`` and COMMIT, so the call to Paystack happens with nothing locked and no other worker
    can claim the same refund. None when there is nothing to send."""
    booking = (db.query(Booking).join(Refund, Refund.booking_id == Booking.id)
               .filter(Refund.status == REFUND_APPROVED, Refund.destination == REFUND_DEST_ORIGINAL)
               .order_by(Refund.approved_at, Refund.id)
               .with_for_update(skip_locked=True, of=Booking).populate_existing().first())
    if booking is None:
        db.rollback()
        return None
    refund = (db.query(Refund).filter(Refund.booking_id == booking.id, Refund.status == REFUND_APPROVED,
                                      Refund.destination == REFUND_DEST_ORIGINAL)
              .with_for_update().populate_existing().first())
    if refund is None:
        db.rollback()
        return _CLAIM_AGAIN
    if not booking.paystack_reference or not refund.amount_kobo:
        # Nothing to send and nothing to ask Paystack: say so where Finance will see it, rather than skip it forever.
        refund.status = REFUND_FAILED
        refund.failure_reason = "There is no Paystack payment on this booking to refund."
        db.commit()
        logger.error("refund %s cannot be sent: no Paystack reference or amount", refund.id)
        return _CLAIM_AGAIN
    attempts = _lock_attempts(db, refund.id)
    attempt = RefundAttempt(refund_id=refund.id, attempt=max((a.attempt for a in attempts), default=0) + 1,
                            status=ATTEMPT_SENDING, created_at=now)
    db.add(attempt)
    refund.status = REFUND_PROCESSING
    refund.failure_reason = None
    db.flush()
    claim = (refund.id, attempt.id, booking.paystack_reference, int(refund.amount_kobo))
    db.commit()
    return claim


def dispatch_due(db: Session, *, paystack: Optional[PaystackService] = None, limit: int = DISPATCH_BATCH,
                 now: Optional[datetime] = None) -> int:
    """Send approved refunds to Paystack (one ``POST /refund`` each). The attempt is committed as ``sending`` first, so two
    workers never send the same one, and the call holds no lock. Silence from Paystack (timeout, 5xx, unreadable reply)
    leaves the attempt ``unknown`` and is NEVER resent: ``reconcile`` decides. Returns how many were sent."""
    paystack = paystack or PaystackService()
    started = time.monotonic()
    sent = 0
    while sent < limit and time.monotonic() - started < DISPATCH_BUDGET_SECONDS:
        claim = _claim_for_send(db, now or datetime.utcnow())
        if claim is None:
            break
        if claim is _CLAIM_AGAIN:
            continue
        refund_id, attempt_id, reference, amount = claim
        try:
            result = paystack.create_refund(reference, amount)
        except Exception:
            logger.exception("refund %s: the call to Paystack failed unexpectedly; the attempt is left unknown", refund_id)
            result = {"outcome": "unknown", "message": "unexpected error"}
        _record_send(db, refund_id, attempt_id, result, now or datetime.utcnow())
        sent += 1
    return sent


def _lock_family(db: Session, refund_id: UUID):
    """Booking, then refund, then its attempts (the lock order) from a refund id. None when the refund is gone."""
    booking_id = db.query(Refund.booking_id).filter(Refund.id == refund_id).scalar()
    if booking_id is None:
        return None
    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().populate_existing().one()
    refund = db.query(Refund).filter(Refund.id == refund_id).with_for_update().populate_existing().one()
    return booking, refund, _lock_attempts(db, refund_id)


def _record_send(db: Session, refund_id: UUID, attempt_id: UUID, result: Dict[str, Any], now: datetime) -> None:
    """Write down what Paystack said to one send. Every change is from-state guarded: a webhook may already have moved the
    attempt on while this call was in flight, and then this only fills in what is missing."""
    try:
        family = _lock_family(db, refund_id)
        if family is None:
            db.rollback()
            return
        booking, refund, attempts = family
        attempt = next(a for a in attempts if a.id == attempt_id)
        outcome = result.get("outcome")
        pid = result.get("paystack_refund_id")
        if outcome == "accepted" and not pid:
            logger.error("refund %s: Paystack accepted the refund but sent no refund id", refund.id)
            outcome = "unknown"
        if outcome == "accepted":
            pid = str(pid)
            if attempt.paystack_refund_id and attempt.paystack_refund_id != pid:
                # An early webhook (or a refund made on the Paystack dashboard) tied this attempt to another Paystack
                # refund, and now the create call returned a different one: two refunds may exist for one attempt.
                _flag_review(db, refund, f"Paystack's reply to the send names refund {pid}, but this attempt was already "
                                         f"tied to refund {attempt.paystack_refund_id}. Two refunds may exist.")
                db.commit()
                return
            if not attempt.paystack_refund_id:
                attempt.paystack_refund_id = pid
            if attempt.status in (ATTEMPT_SENDING, ATTEMPT_UNKNOWN):
                attempt.status = ATTEMPT_PENDING
            if refund.status == REFUND_PROCESSING and attempt is attempts[-1]:
                booking_events.emit(db, booking, "refund_flagged", f"{refund.id}:processing")  # "being processed"
                reported = _normal_status(result.get("status"))
                if reported in ("processing", "needs-attention", "processed", "failed"):
                    _settle_locked(db, booking, refund, attempts, attempt, reported, pid, refund.amount_kobo, None,
                                   booking.paystack_reference, now)
        elif outcome == "refused":
            message = (result.get("message") or "Paystack refused the refund")[:500]
            logger.warning("refund %s refused by Paystack: %s", refund.id, message)
            if attempt.status == ATTEMPT_SENDING and refund.status == REFUND_PROCESSING and attempt is attempts[-1]:
                attempt.status, attempt.failure_reason = ATTEMPT_FAILED, message
                refund.status, refund.failure_reason = REFUND_FAILED, message
        else:
            logger.warning("refund %s: no clear answer from Paystack (%s); left processing, never resent", refund.id,
                           result.get("message"))
            if attempt.status == ATTEMPT_SENDING:
                attempt.status = ATTEMPT_UNKNOWN
        db.commit()
    except Exception:
        db.rollback()
        raise


# --- settling (spec 6.4) --------------------------------------------------------------------------------------------
EVENT_STATUSES = ("pending", "processing", "needs-attention", "processed", "failed")
_ATTEMPT_STATUS = {"pending": ATTEMPT_PENDING, "processing": ATTEMPT_PROCESSING, "needs-attention": ATTEMPT_NEEDS_ATTENTION,
                   "processed": ATTEMPT_PROCESSED, "failed": ATTEMPT_FAILED}
_RANK = {ATTEMPT_SENDING: 0, ATTEMPT_UNKNOWN: 1, ATTEMPT_PENDING: 2, ATTEMPT_PROCESSING: 3, ATTEMPT_NEEDS_ATTENTION: 4}
_NOT_SENT = (REFUND_PENDING_APPROVAL, REFUND_APPROVED)
_FINISHED = (REFUND_FAILED, REFUND_SUCCEEDED, REFUND_CLOSED, REFUND_REJECTED)


def _normal_status(status: Any) -> str:
    return str(status or "").strip().lower().replace("_", "-")


def _kobo(value: Any) -> Optional[int]:
    """Paystack sends amounts as integers or numeric strings; anything else is unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _flag_review(db: Session, refund: Refund, why: str) -> str:
    """Paystack disagrees with what we recorded (spec 4.3). Nothing else moves by itself. The refund goes to ``needs_review``
    unless it is finished AND its booking has another open refund (reopening it would collide with the one-open-refund
    index): then it keeps its status and carries ``review_flagged_at`` / ``review_reason`` for the console queue. The flag is
    always set, so a person can find every contradiction."""
    logger.error("refund %s needs review: %s", refund.id, why)
    refund.review_flagged_at = refund.review_flagged_at or datetime.utcnow()
    refund.review_reason = why[:500]
    if refund.status in REFUND_OPEN_STATUSES:
        refund.status = REFUND_NEEDS_REVIEW
        refund.failure_reason = why[:500]
    elif refund.status in (REFUND_SUCCEEDED, REFUND_CLOSED):
        other_open = (db.query(Refund.id).filter(Refund.booking_id == refund.booking_id, Refund.id != refund.id,
                                                 Refund.status.in_(REFUND_OPEN_STATUSES)).first())
        if other_open is None:
            refund.status = REFUND_NEEDS_REVIEW
            refund.failure_reason = why[:500]
    return "review"


def _settle_locked(db: Session, booking: Booking, refund: Refund, attempts: List[RefundAttempt],
                   target: Optional[RefundAttempt], status: str, pid: Optional[str], amount_kobo: Optional[int],
                   reason: Optional[str], transaction_reference: Optional[str], now: datetime) -> str:
    """Apply one Paystack refund status to a refund whose booking, refund and attempts the caller has locked. Every
    transition happens from one state only, so a repeat is a no-op. Returns applied | duplicate | review."""
    state = refund.status
    if state == REFUND_NEEDS_REVIEW:
        logger.warning("refund %s: Paystack says %s but the refund is awaiting review; nothing moved", refund.id, status)
        return "review"
    if transaction_reference and booking.paystack_reference != transaction_reference:
        return _flag_review(db, refund, f"Paystack reported {status} for transaction {transaction_reference}, which is not "
                                    "this booking's payment.")
    current = attempts[-1] if attempts else None
    if current is None:
        return _flag_review(db, refund, f"Paystack reported {status} for a refund Oakly never sent from this refund. Check "
                                    "the Paystack dashboard.")
    target = target or current
    if target.id != current.id:
        if status == "processed":
            return _flag_review(db, refund, f"Paystack processed attempt {target.attempt}, but attempt {current.attempt} is the "
                                        "current one. Money may have been sent twice.")
        logger.warning("refund %s: ignoring %s for the older attempt %s", refund.id, status, target.attempt)
        return "duplicate"
    if pid and current.paystack_refund_id and current.paystack_refund_id != pid:
        return _flag_review(db, refund, f"Paystack reported {status} for refund {pid}, but this attempt is refund "
                                    f"{current.paystack_refund_id}. Two refunds may exist.")
    if status == "processed" and amount_kobo is None and state != REFUND_SUCCEEDED:
        return _flag_review(db, refund, "Paystack reported the refund processed but the amount could not be read, so it "
                                        "cannot be confirmed. Check the Paystack dashboard.")
    if amount_kobo is not None and refund.amount_kobo is not None and int(amount_kobo) != int(refund.amount_kobo):
        return _flag_review(db, refund, f"Paystack reported {status} for {amount_kobo} kobo, but this refund is "
                                    f"{refund.amount_kobo} kobo.")

    def adopt() -> None:
        if pid and not current.paystack_refund_id:
            current.paystack_refund_id = pid

    if status == "processed":
        if state in (REFUND_PROCESSING, REFUND_NEEDS_ATTENTION):
            adopt()
            current.status = ATTEMPT_PROCESSED
            refund.failure_reason = None
            _mark_succeeded(db, refund, booking, now)
            return "applied"
        if state == REFUND_SUCCEEDED and current.status == ATTEMPT_PROCESSED:
            return "duplicate"
        return _flag_review(db, refund, f"Paystack processed this refund while it was {state}.")
    if status == "failed":
        if state in (REFUND_PROCESSING, REFUND_NEEDS_ATTENTION):
            adopt()
            why = (reason or "Paystack reported the refund as failed")[:500]
            current.status, current.failure_reason = ATTEMPT_FAILED, why
            refund.status, refund.failure_reason = REFUND_FAILED, why
            return "applied"
        if state in (REFUND_FAILED, REFUND_CLOSED, REFUND_REJECTED):
            return "duplicate"
        return _flag_review(db, refund, f"Paystack reported the refund as failed while it was {state}.")
    # pending | processing | needs-attention
    if state in _NOT_SENT:
        return _flag_review(db, refund, f"Paystack reported {status} for a refund Oakly has not sent ({state}).")
    if state in _FINISHED:
        logger.warning("refund %s: ignoring a late %s (the refund is %s)", refund.id, status, state)
        return "duplicate"
    if status == "needs-attention":
        if state == REFUND_NEEDS_ATTENTION:
            return "duplicate"
        adopt()
        current.status = ATTEMPT_NEEDS_ATTENTION
        refund.status, refund.failure_reason = REFUND_NEEDS_ATTENTION, None
        booking_events.emit(db, booking, "refund_needs_bank_details", f"{refund.id}:needs_attention")
        return "applied"
    if state == REFUND_NEEDS_ATTENTION:  # Paystack took the bank details and is processing again
        if status != "processing":
            return "duplicate"
        adopt()
        current.status, refund.status = ATTEMPT_PROCESSING, REFUND_PROCESSING
        return "applied"
    adopt()
    if _RANK.get(current.status, -1) < _RANK[_ATTEMPT_STATUS[status]]:
        current.status = _ATTEMPT_STATUS[status]
        return "applied"
    return "duplicate"


def _locate_for_event(db: Session, transaction_reference: Optional[str], pid: Optional[str]):
    """(booking id, refund id or None, attempt id or None) for a Paystack event, read WITHOUT locks (the caller then locks in
    order and re-checks everything). By Paystack's refund id when an attempt holds it, else by the transaction reference."""
    if pid:
        row = (db.query(Refund.booking_id, Refund.id, RefundAttempt.id).join(RefundAttempt, RefundAttempt.refund_id == Refund.id)
               .filter(RefundAttempt.paystack_refund_id == pid).order_by(RefundAttempt.created_at.desc()).first())
        if row is not None:
            return row
    if transaction_reference:
        ids = db.query(Booking.id).filter(Booking.paystack_reference == transaction_reference).limit(2).all()
        if len(ids) > 1:
            logger.error("refund event: more than one booking has Paystack reference %s; ignored", transaction_reference)
            return None
        if ids:
            return ids[0][0], None, None
    return None


def _already_settled(db: Session, booking: Booking, status: str, pid: Optional[str], amount_kobo: Optional[int]) -> bool:
    """A repeat of a ``processed`` we already applied (the booking's refund is finished, so it is no longer "open")."""
    if status != "processed":
        return False
    for refund in db.query(Refund).filter(Refund.booking_id == booking.id, Refund.status == REFUND_SUCCEEDED).all():
        current = max(refund.attempts, key=lambda a: a.attempt, default=None)
        if (current is not None and current.status == ATTEMPT_PROCESSED and (pid is None or current.paystack_refund_id == pid)
                and (amount_kobo is None or amount_kobo == int(refund.amount_kobo or 0))):
            return True
    return False


def apply_refund_event(db: Session, *, status: str, transaction_reference: Optional[str], paystack_refund_id: Optional[str],
                       amount_kobo: Optional[int], reason: Optional[str] = None, now: Optional[datetime] = None,
                       attempt_id: Optional[UUID] = None, paystack: Optional[PaystackService] = None) -> str:
    """Settle a refund from Paystack's word (a ``refund.*`` webhook, or reconcile). ``status`` is Paystack's refund status:
    pending, processing, needs-attention, processed or failed. Matches by Paystack's refund id, else by the transaction
    reference to the booking's one open refund and its current attempt. Safe to call any number of times for one event.
    Returns ``applied``, ``duplicate``, ``ignored`` (not ours) or ``review`` (Paystack contradicts what we recorded: the refund
    is ``needs_review``). ``attempt_id`` pins the event to the attempt the caller (reconcile) was asking about. A ``processed``
    whose amount is missing or unreadable is looked up at Paystack first (no lock held); still unknown, it is a
    contradiction. Commits."""
    now = now or datetime.utcnow()
    status = _normal_status(status)
    pid = str(paystack_refund_id) if paystack_refund_id not in (None, "") else None
    if status not in EVENT_STATUSES:
        logger.info("refund event with status %r ignored", status)
        return "ignored"
    amount_kobo = _kobo(amount_kobo)
    if status == "processed" and amount_kobo is None and pid:
        got = (paystack or PaystackService()).fetch_refund(pid)
        if got.get("outcome") == "found":
            amount_kobo = _kobo(got.get("amount_kobo"))
    try:
        found = _locate_for_event(db, transaction_reference, pid)
        db.rollback()  # the lookup held no lock worth keeping; start the locked part clean
        if found is None:
            logger.warning("refund event %s for an unknown refund (%s): ignored", status, pid or transaction_reference)
            return "ignored"
        booking_id, refund_id, found_attempt_id = found
        booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().populate_existing().one()
        query = db.query(Refund)
        query = (query.filter(Refund.id == refund_id) if refund_id is not None
                 else query.filter(Refund.booking_id == booking.id, Refund.status.in_(REFUND_OPEN_STATUSES)))
        refund = query.with_for_update().populate_existing().first()
        if refund is None:
            result = "duplicate" if _already_settled(db, booking, status, pid, amount_kobo) else "ignored"
            if result == "ignored":
                logger.warning("refund event %s on booking %s, which has no open refund: ignored", status, booking.id)
            db.rollback()
            return result
        attempts = _lock_attempts(db, refund.id)
        pinned = attempt_id or found_attempt_id
        target = next((a for a in attempts if a.id == pinned), None) if pinned else None
        result = _settle_locked(db, booking, refund, attempts, target, status, pid, amount_kobo, reason,
                                transaction_reference, now)
        db.commit()
        if result == "applied":
            logger.info("refund %s: Paystack %s applied", refund.id, status)
        return result
    except Exception:
        db.rollback()
        raise


# --- reconcile (spec 6.4) -------------------------------------------------------------------------------------------
def _review_unsettled(db: Session, refund_id: UUID, attempt_id: UUID, why: str) -> bool:
    """Put a refund that is still waiting on the same attempt into review. False when it has moved on meanwhile."""
    try:
        family = _lock_family(db, refund_id)
        if family is None:
            db.rollback()
            return False
        _booking, refund, attempts = family
        if (refund.status not in (REFUND_PROCESSING, REFUND_NEEDS_ATTENTION) or not attempts or attempts[-1].id != attempt_id
                or attempts[-1].status not in _RANK):
            db.rollback()
            return False
        _flag_review(db, refund, why)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def reconcile(db: Session, *, paystack: Optional[PaystackService] = None, now: Optional[datetime] = None,
              limit: int = RECONCILE_BATCH) -> int:
    """Ask Paystack about refunds that have been ``processing`` (or ``needs_attention``) for a while (a webhook that never
    came, or a send whose reply was lost) and apply what it says, as the webhook would. A definite answer settles the refund.
    Paystack having NO record 30 minutes after we sent is not proof the refund was not sent, so it is never "failed": the
    refund goes to ``needs_review``. An unreachable Paystack changes nothing. Sends without a Paystack id (sending, unknown)
    are asked about first, so a pile of old ``processing`` refunds cannot starve them. One refund failing does not stop the
    others. Returns how many refunds were acted on."""
    paystack = paystack or PaystackService()
    now = now or datetime.utcnow()
    latest = select(func.max(RefundAttempt.attempt)).where(RefundAttempt.refund_id == Refund.id).correlate(Refund).scalar_subquery()
    due = (db.query(Refund.id, Refund.amount_kobo, RefundAttempt.id, RefundAttempt.paystack_refund_id,
                    RefundAttempt.created_at, Booking.paystack_reference)
           .join(RefundAttempt, RefundAttempt.refund_id == Refund.id).join(Booking, Booking.id == Refund.booking_id)
           .filter(Refund.status.in_((REFUND_PROCESSING, REFUND_NEEDS_ATTENTION)), RefundAttempt.attempt == latest,
                   RefundAttempt.status.in_(tuple(_RANK)), RefundAttempt.created_at <= now - RECONCILE_AFTER)
           .order_by(RefundAttempt.paystack_refund_id.isnot(None), RefundAttempt.created_at).limit(limit).all())
    db.rollback()
    acted = 0
    for item in due:
        try:
            acted += _reconcile_one(db, paystack, now, *item)
        except Exception:
            db.rollback()
            logger.exception("refund %s: reconcile failed; the others carry on", item[0])
    return acted


def _reconcile_one(db: Session, paystack: PaystackService, now: datetime, refund_id: UUID, amount: Optional[int],
                   attempt_id: UUID, pid: Optional[str], sent_at: datetime, reference: Optional[str]) -> int:
    overdue = now - sent_at >= NOT_FOUND_GRACE
    if pid:
        got = paystack.fetch_refund(pid)
        if got.get("outcome") == "found":
            return apply_refund_event(db, status=got.get("status"), transaction_reference=reference, paystack_refund_id=pid,
                                      amount_kobo=got.get("amount_kobo"), now=now, attempt_id=attempt_id,
                                      paystack=paystack) in ("applied", "review")
        if got.get("outcome") == "not_found" and overdue:
            return _review_unsettled(db, refund_id, attempt_id, NO_RECORD_REASON)
        return 0
    if not reference:
        return 0
    listed = paystack.list_refunds(reference)
    if listed.get("outcome") != "found":
        return 0  # Paystack could not be reached: silence is not "no record"
    booking_id = db.query(Refund.booking_id).filter(Refund.id == refund_id).scalar()
    taken = {p for (p,) in db.query(RefundAttempt.paystack_refund_id).join(Refund, Refund.id == RefundAttempt.refund_id)
             .filter(Refund.booking_id == booking_id, RefundAttempt.paystack_refund_id.isnot(None))}
    db.rollback()
    # A transaction can carry several Paystack refunds: ours is one not already tied to an attempt, for our amount.
    candidates = [r for r in listed.get("refunds", []) if r["paystack_refund_id"] not in taken
                  and _kobo(r.get("amount_kobo")) == int(amount or 0)]
    if len(candidates) == 1:
        found = candidates[0]
        return apply_refund_event(db, status=found.get("status"), transaction_reference=reference,
                                  paystack_refund_id=found["paystack_refund_id"], amount_kobo=found.get("amount_kobo"),
                                  now=now, attempt_id=attempt_id, paystack=paystack) in ("applied", "review")
    if len(candidates) > 1:
        return _review_unsettled(db, refund_id, attempt_id,
                                 "Paystack holds more than one refund that could be this attempt. Check the Paystack "
                                 "dashboard before resolving it.")
    return _review_unsettled(db, refund_id, attempt_id, NO_RECORD_REASON) if overdue else 0
