"""Wave dispatch: offer a paid booking to caregivers in batches until someone accepts.

State lives on the booking (``dispatch_state``: dispatching | matched | unmatched, NULL for
bookings paid before this existed) and in ``booking_dispatch_waves``, one row per batch.

- Settlement marks the booking ``dispatching`` in the same transaction as the payment, then calls
  start_dispatch() to send wave 1. If that call fails, dispatch_tick() sends it a minute later.
- dispatch_tick() (ARQ cron, every minute) moves each ``dispatching`` booking along: when the
  latest wave has expired without an accept it notifies the next batch of caregivers who have not
  been offered the booking yet. After DISPATCH_ESCALATE_MINUTES in total, or when nobody is left
  to notify, the booking becomes ``unmatched`` and is logged for the admin unmatched queue.
- Accepting (BookingService.handle_booking_action) sets ``matched`` and stays allowed for any
  caregiver notified in any wave, including after the booking was escalated to ``unmatched``.

Safe with several workers: every step takes the booking row lock (the tick uses SKIP LOCKED, so a
second worker moves on instead of waiting) and re-checks state under it, and
(booking_id, wave_no) is unique, so the same wave can never be sent twice. All jobs take an
explicit ``now`` (naive UTC, like every timestamp in this schema) so tests never sleep.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import (
    Booking,
    BookingDispatchWave,
    BookingNotification,
    BookingStatus,
    PaymentStatus,
)
from app.services.booking import BookingService

logger = logging.getLogger(__name__)

DISPATCHING = "dispatching"
MATCHED = "matched"
UNMATCHED = "unmatched"


def find_candidates(db: Session, booking: Booking, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Everyone eligible for the booking right now, best match first."""
    return BookingService(db).smart_caregiver_matching(booking, now=now)


def _lock(db: Session, booking_id: uuid.UUID, skip_locked: bool = False) -> Optional[Booking]:
    return (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .with_for_update(skip_locked=skip_locked)
        .populate_existing()
        .first()
    )


def _is_open(booking: Booking) -> bool:
    """Still waiting for a caregiver: dispatching, unassigned, pending and paid."""
    return (
        booking.dispatch_state == DISPATCHING
        and booking.status == BookingStatus.PENDING
        and booking.caregiver_id is None
        and booking.payment_status == PaymentStatus.PAID
    )


def _mark_unmatched(booking: Booking, reason: str, wave_count: int) -> None:
    booking.dispatch_state = UNMATCHED
    logger.warning(
        "Booking %s unmatched (%s) after %d wave(s); needs the admin unmatched queue",
        booking.id, reason, wave_count,
    )


def _send_wave(db: Session, booking: Booking, wave_no: int, now: datetime) -> bool:
    """Notify the next batch of candidates. False when there is nobody left to notify."""
    already = {
        cid
        for (ids,) in db.query(BookingDispatchWave.notified_caregiver_ids).filter(
            BookingDispatchWave.booking_id == booking.id
        )
        for cid in ids
    }
    chosen = [c for c in find_candidates(db, booking, now) if c["caregiver_id"] not in already]
    chosen = chosen[: settings.DISPATCH_WAVE_SIZE]
    if not chosen:
        return False

    for c in chosen:
        db.add(BookingNotification(
            booking_id=booking.id,
            caregiver_id=c["caregiver_id"],
            title="New Booking Request",
            message=(
                f"New {booking.service_type} request - {c['distance_km']}km away, "
                f"ETA {c.get('estimated_arrival_minutes', 'N/A')} min"
            ),
            notification_type="new_booking",
        ))
    db.add(BookingDispatchWave(
        booking_id=booking.id,
        wave_no=wave_no,
        notified_caregiver_ids=[c["caregiver_id"] for c in chosen],
        sent_at=now,
        expires_at=now + timedelta(minutes=settings.DISPATCH_WAVE_MINUTES),
    ))
    return True


def _advance(db: Session, booking: Booking, now: datetime) -> bool:
    """Send the next wave or escalate, under the booking lock. True if anything changed."""
    if not _is_open(booking):
        return False

    latest = (
        db.query(BookingDispatchWave)
        .filter(BookingDispatchWave.booking_id == booking.id)
        .order_by(BookingDispatchWave.wave_no.desc())
        .first()
    )
    wave_count = latest.wave_no if latest else 0

    started = booking.dispatch_started_at or now
    if now - started >= timedelta(minutes=settings.DISPATCH_ESCALATE_MINUTES):
        _mark_unmatched(booking, "escalation window elapsed", wave_count)
        return True

    if latest is not None and latest.expires_at > now:
        return False  # the current wave is still open

    if not _send_wave(db, booking, wave_count + 1, now):
        _mark_unmatched(booking, "no candidates left", wave_count)
    return True


def start_dispatch(db: Session, booking_id: uuid.UUID, now: Optional[datetime] = None) -> None:
    """Begin dispatch for a freshly paid booking by sending wave 1. Idempotent: a booking that
    already has a wave (or is no longer waiting for a caregiver) is left alone."""
    now = now or datetime.utcnow()
    booking = _lock(db, booking_id)
    if booking is None:
        db.rollback()
        return
    try:
        if (
            booking.dispatch_state is None
            and booking.status == BookingStatus.PENDING
            and booking.payment_status == PaymentStatus.PAID
        ):
            booking.dispatch_state = DISPATCHING
            booking.dispatch_started_at = now
        has_wave = db.query(BookingDispatchWave.id).filter(
            BookingDispatchWave.booking_id == booking.id
        ).first() is not None
        if not has_wave:
            _advance(db, booking, now)
        db.commit()
    except Exception:
        db.rollback()
        raise


def dispatch_tick(db: Session, now: Optional[datetime] = None) -> int:
    """Advance every booking that is waiting for a caregiver. Returns how many changed.

    A booking another worker currently holds locked is skipped, not waited for.
    """
    now = now or datetime.utcnow()
    open_ids = [
        row.id
        for row in db.query(Booking.id).filter(
            Booking.dispatch_state == DISPATCHING,
            Booking.status == BookingStatus.PENDING,
            Booking.caregiver_id.is_(None),
            Booking.payment_status == PaymentStatus.PAID,
        )
    ]
    db.rollback()  # end the read transaction; each booking gets its own short one

    advanced = 0
    for booking_id in open_ids:
        try:
            booking = _lock(db, booking_id, skip_locked=True)
            if booking is None:
                db.rollback()
                continue
            if _advance(db, booking, now):
                advanced += 1
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("dispatch_tick failed for booking %s", booking_id)
    return advanced
