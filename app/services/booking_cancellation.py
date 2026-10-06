"""The booker's cancellation policy and the cancellation itself (WA-FR-39), independent of the channel.

Policy, in the order it is checked (the same function answers "what would it cost?" before the person confirms and
"cancel it" after, so the two cannot disagree):

| The booking is...                                   | Result                                                  |
|-----------------------------------------------------|---------------------------------------------------------|
| unpaid                                              | not here: the W2 verify-before-cancel path handles it   |
| paid, no caregiver yet                              | cancel, full refund                                     |
| accepted, more than FREE_HOURS before the start     | cancel, full refund                                     |
| accepted, closer than that (or already on the way)  | cancel, refund minus FEE_PERCENT of the *service fee*   |
| in progress                                         | refused: a person has to handle it                      |
| completed / cancelled / declined                    | refused                                                 |

Cancelling a paid booking never moves money. It sets ``refund_required`` (reason ``cancelled_by_booker``) and the
amount to refund in ``refund_amount_kobo``: the refund module (Admin PRD) consumes that queue, exactly as it does
for a payment that arrived after expiry. Money is integer kobo throughout; the fee is rounded half up.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

CANCELLED_BY_BOOKER = "cancelled_by_booker"
REASON_IN_PROGRESS = "in_progress"
REASON_CLOSED = "closed"
REASON_UNPAID = "unpaid"


@dataclass(frozen=True)
class Terms:
    allowed: bool
    refusal: Optional[str] = None     # in_progress | closed | unpaid, when not allowed
    refund_kobo: int = 0
    fee_kobo: int = 0
    assigned: bool = False


def fee_kobo(booking: Booking) -> int:
    percent = max(0, min(100, int(settings.WHATSAPP_CANCEL_FEE_PERCENT)))
    return (int(booking.service_fee_kobo) * percent + 50) // 100


def terms(booking: Booking, now: datetime) -> Terms:
    status = booking.status
    if status == BookingStatus.IN_PROGRESS:
        return Terms(False, REASON_IN_PROGRESS)
    if status not in (BookingStatus.PENDING, BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE):
        return Terms(False, REASON_CLOSED)
    if booking.payment_status == PaymentStatus.PENDING:
        return Terms(False, REASON_UNPAID)
    if booking.payment_status != PaymentStatus.PAID:
        return Terms(False, REASON_CLOSED)
    total = int(booking.total_amount_kobo)
    if status == BookingStatus.PENDING:
        return Terms(True, refund_kobo=total)
    hours_left = (booking.scheduled_date - now).total_seconds() / 3600
    if status == BookingStatus.ACCEPTED and hours_left > settings.WHATSAPP_CANCEL_FREE_HOURS:
        return Terms(True, refund_kobo=total, assigned=True)
    fee = min(fee_kobo(booking), total)
    return Terms(True, refund_kobo=total - fee, fee_kobo=fee, assigned=True)


@dataclass(frozen=True)
class Outcome:
    status: str                       # cancelled | already_cancelled | refused | terms_changed
    terms: Optional[Terms] = None


def cancel_by_booker(db: Session, booking_id: uuid.UUID, now: Optional[datetime] = None, *,
                     commit: bool = True, accepted_fee_kobo: Optional[int] = None) -> Outcome:
    """Cancel a paid booking for its booker. Takes the booking row lock and re-reads everything under it, so a
    caregiver acting at the same moment, or a double tap, gets a single, consistent result.

    ``commit=True`` ends the transaction (committing a cancel, rolling back a refusal, both release the lock).
    ``commit=False`` is for a caller that owns the transaction (the chat step holds the conversation lock and
    commits once at the end): nothing is committed or rolled back here.

    ``accepted_fee_kobo`` is the fee the person was shown and agreed to. When given, the booking is only cancelled if
    the fee decided *under the lock* is no higher than that; otherwise nothing changes and the outcome is
    ``terms_changed`` with the new terms (a caregiver may have accepted, or the 12 h line passed, since they looked)."""
    now = now or datetime.utcnow()

    def release() -> None:
        if commit:
            db.rollback()

    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().populate_existing().first()
    if booking is None:
        release()
        return Outcome("refused", Terms(False, REASON_CLOSED))
    if booking.status == BookingStatus.CANCELLED:
        release()
        return Outcome("already_cancelled")
    decided = terms(booking, now)
    if not decided.allowed:
        release()
        return Outcome("refused", decided)
    if accepted_fee_kobo is not None and decided.fee_kobo > accepted_fee_kobo:
        release()
        return Outcome("terms_changed", decided)
    booking.status = BookingStatus.CANCELLED
    booking.cancel_reason = CANCELLED_BY_BOOKER
    flag_refund(booking, CANCELLED_BY_BOOKER, decided.refund_kobo)
    NotificationService(db).cancelled_by_booker(booking)  # the caregiver, if one was assigned: same transaction
    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("booking %s cancelled by its booker (refund %s kobo, fee %s kobo)", booking.booking_code,
                decided.refund_kobo, decided.fee_kobo)
    return Outcome("cancelled", decided)


def flag_refund(booking: Booking, reason: str, amount_kobo: Optional[int]) -> None:
    """Put the booking in the admin refund queue. ``amount_kobo`` None means "a person decides the amount"."""
    booking.refund_required = True
    booking.refund_required_reason = reason
    booking.refund_amount_kobo = amount_kobo
