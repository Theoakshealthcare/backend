"""Refunds v2 (docs/plans/2026-10-21-refunds-v2-design.md): creating refund rows, and what the policy suggests for each.

This is the creation half. A refund row is made ``pending_approval`` by a cancel path (``open_system_refund``, called by
``booking_cancellation.flag_refund`` and the late-payment path) or by staff (``request_refund``). Later tasks add approval and
sending to this module. Money is integer kobo; the Paystack fee is never deducted from a suggestion.

Lock order, as everywhere in the money code: booking, refund, refund attempts, caregiver ledger rows, wallets.
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.admin import AdminUser
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import TX_EARNING, CaregiverTransaction
from app.db.models.patient import Patient
from app.db.models.refund import (
    REFUND_APPROVED, REFUND_COMMITTED_STATUSES, REFUND_DEST_ORIGINAL, REFUND_DEST_WALLET, REFUND_OPEN_STATUSES,
    REFUND_ORIGIN_STAFF, REFUND_ORIGIN_SYSTEM, REFUND_PENDING_APPROVAL, REFUND_REASON_CODES, REFUND_REJECTED,
    REFUND_SUCCEEDED, Refund,
)
from app.db.models.user import User
from app.services import booking_events, booking_policy, patient_wallet
from app.services.audit import AuditService
from app.services.booking_cancellation import TIER_LATE, TIER_STANDARD, CancelPolicy, _fee

logger = logging.getLogger(__name__)

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
    """Placeholder for ``earnings.apply_take_back`` (Task 6). Refuses rather than silently skip a take-back."""
    raise NotImplementedError("caregiver take-back is not implemented yet")


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
        raise RefundInvalid(f"The amount must be between 1 and {refundable} kobo, what is left to refund")

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
        patient_wallet.credit_refund_locked(db, booking.patient_id, amount, refund.id)
        _mark_succeeded(db, refund, booking, now)
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
