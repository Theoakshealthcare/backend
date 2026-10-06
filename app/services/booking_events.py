"""Record that something happened to a booking that someone may need telling (the ``booking_events`` outbox).

Domain code calls ``emit`` inside the transaction that makes the change, so the event exists if and only if the
change committed. It never sends anything: a worker delivers events (``app.channels.whatsapp.notifications``).

Who an event is for decides whether it is recorded at all, so rows never pile up with nobody to read them:
- a booker event is only recorded for a WhatsApp booking (the booker is on WhatsApp);
- a caregiver event is only recorded when a caregiver is assigned (``key`` carries which one).
Domain code does not call this directly for the W3 journey: ``app.services.notification_service`` turns a domain
change ("the caregiver accepted") into the right events.
"""

import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.booking import Booking
from app.db.models.whatsapp_booking import BOOKING_EVENTS, CAREGIVER_EVENTS, BookingEvent


def emit(db: Session, booking: Booking, event: str, key: str = "") -> bool:
    """Queue ``event`` for ``booking`` unless the same ``(event, key)`` is already queued or sent. Returns True when
    a row was added. Does not commit."""
    if event not in BOOKING_EVENTS:
        raise ValueError(f"unknown booking event {event!r}")
    if event in CAREGIVER_EVENTS:
        if booking.caregiver_id is None and not key:
            return False
    elif booking.channel != "whatsapp":
        return False
    inserted = db.execute(
        insert(BookingEvent)
        .values(id=uuid.uuid4(), booking_id=booking.id, event=event, dedupe_key=key, created_at=datetime.utcnow())
        .on_conflict_do_nothing(constraint="uq_booking_events_booking_event").returning(BookingEvent.id)
    ).first()
    return inserted is not None
