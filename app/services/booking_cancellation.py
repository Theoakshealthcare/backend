"""The booker's cancellation policy and the cancellation itself (WA-FR-39), independent of the channel.

Policy, in the order it is checked (the same function answers "what would it cost?" before the person confirms and
"cancel it" after, so the two cannot disagree). The tiers come from the booking's own snapshot of the admin policy (taken
when it was quoted), else the live policy:

| The booking is...                                              | Tier       | Result                                  |
|----------------------------------------------------------------|------------|-----------------------------------------|
| unpaid                                                         |            | not here: W2's verify-before-cancel     |
| paid, no caregiver yet                                         | none       | cancel, full refund                     |
| accepted, more than ``free_hours`` before the start            | none       | cancel, full refund                     |
| accepted, between ``late_hours`` and ``free_hours``            | standard   | refund minus ``fee_percent`` of the service fee |
| accepted and within ``late_hours`` (or past the start), or the |            |                                         |
| caregiver is on the way                                        | late       | refund minus ``late_fee_percent`` of the service fee |
| in progress                                                    |            | refused: a person has to handle it      |
| completed / cancelled / declined                               |            | refused                                 |

Cancelling a paid booking never moves money. It sets ``refund_required`` (reason ``cancelled_by_booker``) and the
amount to refund in ``refund_amount_kobo``: the refund module (Admin PRD) consumes that queue. It also stores the tier
that applied (``cancel_fee_tier``) and, for the late tier, whether it was due to a *qualifying* on-my-way
(``late_cancel_compensable``: tapped in good time and followed by real movement toward the patient, see
``app.services.onmyway``), decided under the booking lock. Paying the caregiver is not done here (A3). Money is integer
kobo throughout; the fee is rounded half up.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.booking import Booking, BookingStatus, CaregiverLocation, PaymentStatus
from app.services import booking_policy, onmyway
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

CANCELLED_BY_BOOKER = "cancelled_by_booker"
REASON_IN_PROGRESS = "in_progress"
REASON_CLOSED = "closed"
REASON_UNPAID = "unpaid"


TIER_NONE, TIER_STANDARD, TIER_LATE = "none", "standard", "late"


@dataclass(frozen=True)
class CancelPolicy:
    free_hours: int
    late_hours: int
    fee_percent: int
    late_fee_percent: int

    @classmethod
    def from_values(cls, values: booking_policy.PolicyValues) -> "CancelPolicy":
        return cls(**booking_policy.cancel_snapshot(values))

    @classmethod
    def from_snapshot(cls, snapshot: Optional[dict]) -> Optional["CancelPolicy"]:
        """The booking's own copy, or None when it has none (or a damaged one): the caller then uses the live policy."""
        try:
            return cls(**{key: int(snapshot[key]) for key in booking_policy.SNAPSHOT_KEYS})  # type: ignore[index]
        except (TypeError, KeyError, ValueError):
            return None

    def percent(self, tier: str) -> int:
        return max(0, min(100, self.late_fee_percent if tier == TIER_LATE else self.fee_percent))


@dataclass(frozen=True)
class Terms:
    allowed: bool
    refusal: Optional[str] = None     # in_progress | closed | unpaid, when not allowed
    refund_kobo: int = 0
    fee_kobo: int = 0
    assigned: bool = False
    tier: str = TIER_NONE
    fee_percent: int = 0              # the percent that produced fee_kobo (of the service fee)
    policy: Optional[CancelPolicy] = None  # the tiers the terms came from, for the wording shown to the person


def _fee(booking: Booking, percent: int) -> int:
    return (int(booking.service_fee_kobo) * percent + 50) // 100


def policy_for(db: Session, booking: Booking) -> CancelPolicy:
    return CancelPolicy.from_snapshot(booking.cancel_policy) or CancelPolicy.from_values(booking_policy.current(db))


def terms_for(db: Session, booking: Booking, now: datetime) -> Terms:
    return terms(booking, now, policy_for(db, booking))


def terms(booking: Booking, now: datetime, policy: CancelPolicy) -> Terms:
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
        return Terms(True, refund_kobo=total, policy=policy)
    hours_left = (booking.scheduled_date - now).total_seconds() / 3600
    if status == BookingStatus.EN_ROUTE or hours_left <= policy.late_hours:
        tier = TIER_LATE  # on the way, or inside the late window (this includes a start time that has passed)
    elif hours_left <= policy.free_hours:
        tier = TIER_STANDARD
    else:
        return Terms(True, refund_kobo=total, assigned=True, policy=policy)
    percent = policy.percent(tier)
    fee = min(_fee(booking, percent), total)
    return Terms(True, refund_kobo=total - fee, fee_kobo=fee, assigned=True, tier=tier, fee_percent=percent, policy=policy)


def _qualifying_on_my_way(db: Session, booking: Booking) -> bool:
    """Late tier only: did the caregiver tap in good time and then really move toward the patient? Reads the
    caregiver's latest location and the live window; the caller holds the booking lock."""
    if booking.status != BookingStatus.EN_ROUTE or booking.caregiver_id is None:
        return False
    ping = db.query(CaregiverLocation).filter(CaregiverLocation.caregiver_id == booking.caregiver_id).first()
    if ping is None or booking.patient_latitude is None or booking.patient_longitude is None:
        return False  # no ping, or no address left to measure against (an erased patient)
    window = booking_policy.current(db).onmyway_compensation_window_minutes
    return onmyway.qualifies(
        on_my_way_at=booking.on_my_way_at, scheduled_date=booking.scheduled_date, window_minutes=window,
        start_distance_m=booking.on_my_way_distance_m, ping_at=ping.updated_at,
        ping_distance_m=onmyway.distance_m(ping.latitude, ping.longitude, booking.patient_latitude,
                                           booking.patient_longitude))


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
    decided = terms_for(db, booking, now)
    if not decided.allowed:
        release()
        return Outcome("refused", decided)
    if accepted_fee_kobo is not None and decided.fee_kobo > accepted_fee_kobo:
        release()
        return Outcome("terms_changed", decided)
    compensable = _qualifying_on_my_way(db, booking) if decided.tier == TIER_LATE else None  # while still en route
    booking.status = BookingStatus.CANCELLED
    booking.cancel_reason = CANCELLED_BY_BOOKER
    booking.cancel_fee_tier = decided.tier
    booking.late_cancel_compensable = compensable
    flag_refund(booking, CANCELLED_BY_BOOKER, decided.refund_kobo)
    NotificationService(db).cancelled_by_booker(booking)  # the caregiver, if one was assigned: same transaction
    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("booking %s cancelled by its booker (tier %s, refund %s kobo, fee %s kobo)", booking.booking_code,
                decided.tier, decided.refund_kobo, decided.fee_kobo)
    return Outcome("cancelled", decided)


def flag_refund(booking: Booking, reason: str, amount_kobo: Optional[int]) -> None:
    """Put the booking in the admin refund queue. ``amount_kobo`` None means "a person decides the amount"."""
    booking.refund_required = True
    booking.refund_required_reason = reason
    booking.refund_amount_kobo = amount_kobo
