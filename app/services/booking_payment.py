"""Single settlement path for booking payments.

Both the Paystack webhook and POST /booking/booking/{id}/verify-payment call
settle_booking_payment(), so the rules cannot drift apart:

- the booking row is locked, so concurrent callers serialise;
- an already-paid booking is a no-op (no state change, no caregiver notification);
- the amount Paystack reports (kobo) must equal the booking total, otherwise the booking
  is NOT marked paid and the discrepancy is recorded in ``payment_failure_reason`` for review;
- caregivers are notified exactly once: only the caller that performs the unpaid -> paid
  transition does it.
"""

import logging
import uuid
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from app.core.money import to_kobo, to_kobo_or_none
from app.db.models.booking import Booking, BookingStatus, PaymentStatus

logger = logging.getLogger(__name__)


class SettleOutcome(str, Enum):
    NOT_FOUND = "not_found"
    ALREADY_PAID = "already_paid"
    AMOUNT_MISMATCH = "amount_mismatch"
    PAID = "paid"  # transitioned to paid now, caregivers notified
    PAID_NOT_DISPATCHED = "paid_not_dispatched"  # money received for a cancelled booking


def expected_kobo(booking: Booking) -> int:
    return to_kobo(booking.total_amount)


naira_to_kobo = to_kobo_or_none


def settle_booking_payment(db: Session, booking_id: uuid.UUID, paid_kobo: Optional[int]) -> SettleOutcome:
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
    cancelled = booking.status == BookingStatus.CANCELLED
    db.commit()

    if cancelled:
        logger.error("Booking %s was cancelled but payment arrived; needs a refund review", booking_id)
        return SettleOutcome.PAID_NOT_DISPATCHED

    # Imported lazily: the booking service pulls in matching/websocket modules.
    from app.services.booking import BookingService

    try:
        notifications = BookingService(db).notify_nearby_caregivers(booking)
        logger.info("Booking %s paid; notified %d caregivers", booking_id, len(notifications))
    except Exception:
        # Payment is already committed; a retry would be a no-op, so surface it loudly instead.
        db.rollback()
        logger.exception("Booking %s paid but caregiver notification failed", booking_id)
    return SettleOutcome.PAID
