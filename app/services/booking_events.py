"""Record that something happened to a booking that the person may need telling (the ``booking_events`` outbox).

Domain code calls ``emit`` inside the transaction that makes the change, so the event exists if and only if the
change committed. It never sends anything: a worker delivers events (``app.channels.whatsapp.notifications``).
Only WhatsApp bookings are of interest today; app bookings are skipped here so their rows never accumulate.
"""

import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.booking import Booking
from app.db.models.whatsapp_booking import BOOKING_EVENTS, BookingEvent


def emit(db: Session, booking: Booking, event: str) -> bool:
    """Queue ``event`` for ``booking`` unless one is already queued or sent. Returns True when a row was added.
    Does not commit."""
    if event not in BOOKING_EVENTS:
        raise ValueError(f"unknown booking event {event!r}")
    if booking.channel != "whatsapp":
        return False
    inserted = db.execute(
        insert(BookingEvent).values(id=uuid.uuid4(), booking_id=booking.id, event=event, created_at=datetime.utcnow())
        .on_conflict_do_nothing(constraint="uq_booking_events_booking_event").returning(BookingEvent.id)
    ).first()
    return inserted is not None
