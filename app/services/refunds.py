"""Refunds v2 (docs/plans/2026-10-21-refunds-v2-design.md): creating refund rows, and what the policy suggests for each.

This is the creation half. A refund row is made ``pending_approval`` by a cancel path (``open_system_refund``, called by
``booking_cancellation.flag_refund`` and the late-payment path) or by staff (``request_refund``). Later tasks add approval and
sending to this module. Money is integer kobo; the Paystack fee is never deducted from a suggestion.

Lock order, as everywhere in the money code: booking, refund, refund attempts, caregiver ledger rows, wallets.
"""
import logging
from typing import Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.admin import AdminUser
from app.db.models.booking import Booking, PaymentStatus
from app.db.models.refund import (
    REFUND_COMMITTED_STATUSES, REFUND_OPEN_STATUSES, REFUND_ORIGIN_STAFF, REFUND_ORIGIN_SYSTEM, REFUND_PENDING_APPROVAL,
    REFUND_REASON_CODES, Refund,
)
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
    if booking.payment_status != PaymentStatus.PAID or _open_refund(db, booking.id) is not None:
        return None
    refund = Refund(
        booking_id=booking.id, patient_id=booking.patient_id, reason_code=reason_code, origin=REFUND_ORIGIN_SYSTEM,
        suggested_amount_kobo=suggested_amount_kobo(booking, reason_code, entered_kobo),
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
