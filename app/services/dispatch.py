"""Wave dispatch: offer a paid booking to caregivers in batches until someone accepts.

State lives on the booking (``dispatch_state``: dispatching | matched | unmatched, NULL for
bookings paid before this existed) and in ``booking_dispatch_waves``, one row per batch.

- Settlement marks the booking ``dispatching`` in the same transaction as the payment, then calls
  start_dispatch() to send wave 1. If that call fails, dispatch_tick() sends it a minute later.
- dispatch_tick() (ARQ cron, every minute) moves each ``dispatching`` booking along: when the
  latest wave has expired without an accept it notifies the next batch of caregivers who have not
  been offered the booking yet. After DISPATCH_ESCALATE_MINUTES in total, or when nobody is left
  to notify after at least one wave, the booking becomes ``unmatched`` and is logged for the admin unmatched queue.
- Once a wave is saved, each notified caregiver with a push token gets a "New visit request" push
  (best effort: a failed push never affects dispatch, see _push_offers).
- Accepting (BookingService.handle_booking_action) sets ``matched`` and stays allowed for any
  caregiver notified in any wave, including after the booking was escalated to ``unmatched``.

Safe with several workers: every step takes the booking row lock (the tick uses SKIP LOCKED, so a
second worker moves on instead of waiting) and re-checks state under it, and
(booking_id, wave_no) is unique, so the same wave can never be sent twice. All jobs take an
explicit ``now`` (naive UTC, like every timestamp in this schema) so tests never sleep.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import (
    Booking,
    BookingDispatchWave,
    BookingNotification,
    BookingStatus,
    PaymentStatus,
)
from app.db.models.caregiver import Caregiver
from app.services import push as push_service
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


def _send_wave(db: Session, booking: Booking, wave_no: int, now: datetime,
               offers: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Notify the next batch of candidates. False when there is nobody left to notify.

    When ``offers`` is given, the pushes this wave owes are appended to it. They are only sent once
    the caller's transaction is done (see _push_offers), never from inside it.
    """
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
    if offers is not None:
        offers.append({
            "booking_id": booking.id,
            "caregiver_ids": [c["caregiver_id"] for c in chosen],
            "service_type": booking.service_type,
            "area": booking.patient_city,
            "scheduled_date": booking.scheduled_date,
        })
    return True


def _push_offers(db: Session, offers: List[Dict[str, Any]]) -> None:
    """Tell the caregivers a wave notified about the new visit. Best effort, after the wave is
    saved: whatever goes wrong here is logged and dispatch carries on (the wave is already recorded,
    and the app still sees the offer by polling). The payload is deliberately minimal: booking id,
    area and time, never the patient's name, condition, address or instructions."""
    for offer in offers:
        try:
            user_ids = [
                row[0] for row in db.query(Caregiver.user_id).filter(Caregiver.id.in_(offer["caregiver_ids"]))
            ]
            scheduled = offer["scheduled_date"]
            local = scheduled.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.HOME_TIMEZONE))
            push_service.get_push_service().send_to_users(
                db, user_ids,
                title="New visit request",
                body=f"{offer['service_type'].capitalize()} visit in {offer['area']}, {local:%a %d %b, %H:%M}",
                data={
                    "type": "job_offer",
                    "booking_id": str(offer["booking_id"]),
                    "area": offer["area"],
                    "scheduled_at": scheduled.isoformat() + "Z",
                },
            )
        except Exception:
            db.rollback()
            logger.warning("push: job-offer push for booking %s failed; dispatch is unaffected",
                           offer.get("booking_id"), exc_info=True)


def _advance(db: Session, booking: Booking, now: datetime,
             offers: Optional[List[Dict[str, Any]]] = None) -> bool:
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

    if not _send_wave(db, booking, wave_count + 1, now, offers):
        if wave_count == 0:
            return False  # nobody online yet: stay dispatching, retry next tick until the window ends
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
    offers: List[Dict[str, Any]] = []
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
            _advance(db, booking, now, offers)
        db.commit()
    except Exception:
        db.rollback()
        raise
    _push_offers(db, offers)


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
            offers: List[Dict[str, Any]] = []
            if _advance(db, booking, now, offers):
                advanced += 1
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("dispatch_tick failed for booking %s", booking_id)
            continue
        _push_offers(db, offers)
    return advanced
