"""Single settlement path for booking payments.

Both the Paystack webhook and POST /booking/booking/{id}/verify-payment call
settle_booking_payment(), so the rules cannot drift apart:

- the booking row is locked, so concurrent callers serialise;
- an already-paid booking is a no-op (no state change, no caregiver notification);
- the amount Paystack reports (kobo) must equal the booking total, otherwise the booking
  is NOT marked paid and the discrepancy is recorded in ``payment_failure_reason`` for review;
- a payment that arrives after the unpaid-expiry cancel reinstates the booking when the visit is at
  least LATE_PAYMENT_REINSTATE_MIN_LEAD_MINUTES away; otherwise (or for any other cancelled booking)
  it stays cancelled and ``refund_required`` is set for the admin refund queue;
- a payment for a booking that stays cancelled records ``payment_refund_pending`` (same transaction), so a
  WhatsApp booker is told a refund is coming instead of hearing nothing;
- a WhatsApp booking flagged ``needs_address_check`` is paid but not dispatched until an operator clears it;
- the ``payment_received`` event is recorded in the same transaction as the payment (the outbox);
- dispatch starts exactly once: only the caller that performs the unpaid -> paid transition marks
  the booking ``dispatching`` (inside the same transaction as the payment) and sends wave 1. If
  sending fails, dispatch_tick() sends it on its next run.
"""

import logging
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.services import booking_events

logger = logging.getLogger(__name__)


class SettleOutcome(str, Enum):
    NOT_FOUND = "not_found"
    ALREADY_PAID = "already_paid"
    AMOUNT_MISMATCH = "amount_mismatch"
    PAID = "paid"  # transitioned to paid now, dispatch started
    PAID_NOT_DISPATCHED = "paid_not_dispatched"  # money received for a cancelled booking
    UNREACHABLE = "unreachable"  # verification only: Paystack could not be asked, so "not paid" is NOT known


def expected_kobo(booking: Booking) -> int:
    return booking.total_amount_kobo


def settle_booking_payment(
    db: Session, booking_id: uuid.UUID, paid_kobo: Optional[int], now: Optional[datetime] = None,
    fees_kobo: Optional[int] = None,
) -> SettleOutcome:
    now = now or datetime.utcnow()
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if booking is None:
        db.rollback()
        return SettleOutcome.NOT_FOUND

    if booking.payment_status == PaymentStatus.PAID:
        db.rollback()
        return SettleOutcome.ALREADY_PAID

    expected = expected_kobo(booking)
    if paid_kobo != expected:
        booking.payment_failure_reason = (
            f"Amount mismatch: expected {expected} kobo, paystack reported {paid_kobo}"
        )
        db.commit()
        logger.error("Booking %s payment amount mismatch: expected %s, got %s", booking_id, expected, paid_kobo)
        return SettleOutcome.AMOUNT_MISMATCH

    booking.payment_status = PaymentStatus.PAID
    booking.payment_failure_reason = None
    if isinstance(fees_kobo, int) and not isinstance(fees_kobo, bool) and fees_kobo >= 0:
        booking.paystack_fee_kobo = fees_kobo  # Paystack's fee, kept for the refund cost-of-fees rule
    cancelled = booking.status == BookingStatus.CANCELLED
    if cancelled:
        lead = timedelta(minutes=settings.LATE_PAYMENT_REINSTATE_MIN_LEAD_MINUTES)
        if booking.cancel_reason == "unpaid_timeout" and booking.scheduled_date > now + lead:
            # Paid after the expiry cancel but with time to spare: put the booking back, same
            # transaction as the payment, and dispatch it like a normal settlement.
            booking.status = BookingStatus.PENDING
            booking.cancel_reason = None
            cancelled = False
            logger.info("Booking %s paid after expiry; reinstated and dispatching", booking_id)
        else:
            booking.refund_required = True
            booking.refund_required_reason = (
                "paid_after_expiry" if booking.cancel_reason == "unpaid_timeout" else "paid_after_cancellation"
            )
            booking_events.emit(db, booking, "payment_refund_pending")  # the booker is told a refund is coming
    held_for_address = False
    if not cancelled:
        # A WhatsApp booking with no map pin has placeholder coordinates: dispatching it would offer the job to
        # the wrong caregivers. It is paid and held until an operator has confirmed the address (WA-FR-14).
        held_for_address = bool(booking.needs_address_check)
        if booking.dispatch_state is None and not held_for_address:
            booking.dispatch_state = "dispatching"
            booking.dispatch_started_at = now
        booking_events.emit(db, booking, "payment_received")  # same transaction as the payment itself
    db.commit()

    if cancelled:
        logger.error("Booking %s was cancelled but payment arrived; needs a refund review", booking_id)
        return SettleOutcome.PAID_NOT_DISPATCHED

    if held_for_address:
        logger.warning("Booking %s paid but held: the address needs a check before dispatch", booking_id)
        return SettleOutcome.PAID

    # Imported lazily: dispatch pulls in the booking service (matching, websocket modules).
    from app.services import dispatch

    try:
        dispatch.start_dispatch(db, booking_id, now)
        logger.info("Booking %s paid; dispatch started", booking_id)
    except Exception:
        # Payment and dispatch_state are already committed, and the tick retries wave 1, so surface
        # it loudly instead of failing the settlement.
        db.rollback()
        logger.exception("Booking %s paid but starting dispatch failed; dispatch_tick will retry", booking_id)
    return SettleOutcome.PAID


def verify_and_settle(db: Session, booking_id: uuid.UUID) -> Optional[SettleOutcome]:
    """Ask Paystack whether this booking was paid and, if so, settle it (the same function the webhook uses).
    Returns the settle outcome; ``UNREACHABLE`` when Paystack could not be asked (callers must not treat that as
    "not paid"); or None when there was nothing to settle (already paid, no reference, or Paystack says not paid). The HTTP call runs with no transaction open and no row lock
    held; ``settle_booking_payment`` then takes the booking lock itself."""
    from app.services.paystack import PaystackService  # lazy: the webhook path imports this module at startup

    booking = db.query(Booking).filter(Booking.id == booking_id).populate_existing().first()
    if booking is None or booking.payment_status == PaymentStatus.PAID or not booking.paystack_reference:
        db.rollback()
        return None
    reference = booking.paystack_reference
    db.rollback()
    verified = PaystackService().verify_transaction(reference)
    if verified.get("unreachable"):
        logger.warning("Booking %s could not be verified: Paystack unreachable", booking_id)
        return SettleOutcome.UNREACHABLE
    data = verified.get("data") or {}
    if not (verified.get("status") and data.get("status") == "success"):
        return None
    outcome = settle_booking_payment(db, booking_id, data.get("amount_kobo"), fees_kobo=data.get("fees_kobo"))
    logger.info("Booking %s verified with Paystack: %s", booking_id, outcome.value)
    return outcome
