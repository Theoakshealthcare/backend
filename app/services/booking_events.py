"""Record that something happened to a booking that someone may need telling (the ``booking_events`` outbox).

Domain code calls ``emit`` inside the transaction that makes the change, so the event exists if and only if the
change committed. It never sends anything: a worker delivers events (``app.channels.whatsapp.notifications``).

Who an event is for decides whether it is recorded at all, so rows never pile up with nobody to read them:
- a booker event is recorded for a WhatsApp booking (the booker is on WhatsApp) and, for the events in
  ``APP_BOOKER_EVENTS``, for an app booking (the booker is in the app: a feed row and a push). A booking is never
  announced on a channel its booker is not on, and ``APP_ONLY_EVENTS`` are never recorded for a WhatsApp booking;
- a caregiver event is only recorded when a caregiver is assigned (``key`` carries which one).
Domain code does not call this directly for the W3 journey: ``app.services.notification_service`` turns a domain
change ("the caregiver accepted") into the right events.

For an app booking the insert runs in a savepoint and a failure is logged, not raised: telling the patient in the app
is a courtesy that must never fail or roll back the booking change it comes from.
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.booking import Booking
from app.db.models.whatsapp_booking import (
    APP_BOOKER_EVENTS, APP_ONLY_EVENTS, BOOKING_EVENTS, CAREGIVER_EVENTS, BookingEvent,
)

logger = logging.getLogger(__name__)


def _insert(db: Session, booking: Booking, event: str, key: str) -> bool:
    inserted = db.execute(
        insert(BookingEvent)
        .values(id=uuid.uuid4(), booking_id=booking.id, event=event, dedupe_key=key, created_at=datetime.utcnow())
        .on_conflict_do_nothing(constraint="uq_booking_events_booking_event").returning(BookingEvent.id)
    ).first()
    return inserted is not None


def emit(db: Session, booking: Booking, event: str, key: str = "") -> bool:
    """Queue ``event`` for ``booking`` unless the same ``(event, key)`` is already queued or sent. Returns True when
    a row was added. Does not commit."""
    if event not in BOOKING_EVENTS:
        raise ValueError(f"unknown booking event {event!r}")
    if event in CAREGIVER_EVENTS:
        if booking.caregiver_id is None and not key:
            return False
    elif booking.channel == "whatsapp":
        if event in APP_ONLY_EVENTS:
            return False
    elif booking.channel == "app":
        if event not in APP_BOOKER_EVENTS:
            return False
        db.flush()  # the caller's own pending changes fail as they always did, not inside the guard below
        try:
            with db.begin_nested():
                return _insert(db, booking, event, key)
        except Exception:
            logger.exception("app notice %s for booking %s was not recorded; the booking is unaffected",
                             event, booking.booking_code)
            return False
    else:
        return False
    return _insert(db, booking, event, key)
