"""Cancel bookings whose payment link expired without payment.

A booking is expirable when it is ``pending`` with payment ``pending`` and
``payment_expires_at`` (set at creation to now + PAYMENT_LINK_TTL_MINUTES) has passed. Rows created
before the column existed have no expiry, so created_at + the TTL is used for them.

Concurrency: each booking is locked (SKIP LOCKED) and re-checked under the lock, so it cannot race
a settlement. If the payment wins, the booking is paid and left alone. If the cancel wins, a late
Paystack charge.success is still recorded by settle_booking_payment (paid, logged for refund
review, not dispatched). Running twice is a no-op: cancelled rows no longer match.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.services import booking_events

logger = logging.getLogger(__name__)

CANCEL_REASON_UNPAID_TIMEOUT = "unpaid_timeout"


def _expired_filters(now: datetime):
    expires_at = func.coalesce(
        Booking.payment_expires_at,
        Booking.created_at + timedelta(minutes=settings.PAYMENT_LINK_TTL_MINUTES),
    )
    return (
        Booking.status == BookingStatus.PENDING,
        Booking.payment_status == PaymentStatus.PENDING,
        expires_at <= now,
    )


def expire_unpaid_bookings(db: Session, now: Optional[datetime] = None) -> int:
    """Cancel expired unpaid bookings. Returns how many were cancelled."""
    now = now or datetime.utcnow()
    candidate_ids = [row.id for row in db.query(Booking.id).filter(*_expired_filters(now))]
    db.rollback()  # end the read transaction; each booking gets its own short one

    cancelled = 0
    for booking_id in candidate_ids:
        try:
            booking = (
                db.query(Booking)
                .filter(Booking.id == booking_id, *_expired_filters(now))
                .with_for_update(skip_locked=True)
                .populate_existing()
                .first()
            )
            if booking is None:  # paid, cancelled or locked by someone else in the meantime
                db.rollback()
                continue
            booking.status = BookingStatus.CANCELLED
            booking.cancel_reason = CANCEL_REASON_UNPAID_TIMEOUT
            booking_events.emit(db, booking, "booking_cancelled_unpaid")  # same transaction as the cancel
            db.commit()
            cancelled += 1
            logger.info("Booking %s cancelled: payment not received in time", booking_id)
        except Exception:
            db.rollback()
            logger.exception("expire_unpaid_bookings failed for booking %s", booking_id)
    return cancelled
