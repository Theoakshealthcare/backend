"""Telling an app patient what happened to their booking: an in-app feed row and, when a device is registered, an
Expo push (the app half of the outbox; the WhatsApp half is ``app.channels.whatsapp.notifications``).

The booking code records an event (``booking_events``, app bookings only, see ``booking_events.emit``) in the same
transaction as the change. The worker then calls ``deliver`` for each such event, which:

1. checks the event is still true of the booking as it is now (a caregiver who was since replaced is not announced);
2. writes the feed row and commits it. ``notifications.event_id`` is unique, so a retry never writes a second row;
3. pushes the same words to the patient's devices. A push never raises and never undoes the feed row.

Because this runs in the worker, outside the booking's transaction, nothing here can fail or roll back the booking
action that caused it.

**Lock-screen safe by construction.** The wording below is the only thing that reaches a lock screen or the feed, and
its only inputs are a caregiver's first name and the booking's ids. It never reads the visit code (the app shows it
after sign-in), the address, the condition, vitals, or the booking code.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.channels.whatsapp.booking_notices import _STALE_FOR, _caregiver_for, _first_name, _refund_for
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.notification import Notification
from app.db.models.patient import Patient
from app.db.models.user import User
from app.db.models.whatsapp_booking import BookingEvent
from app.services import push as push_service

logger = logging.getLogger(__name__)

TARGET_BOOKING = "booking_detail"  # the screen the app opens from the notice
_HAS_CAREGIVER = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)


@dataclass(frozen=True)
class Content:
    type: str
    title: str
    body: str


def recipient_user_id(db: Session, booking: Booking) -> Optional[uuid.UUID]:
    """The app booker's account: for an app booking the patient is the booker (``booked_by_user_id`` stays NULL)."""
    if booking.channel != "app":
        return None  # a WhatsApp booker is told on WhatsApp, never here
    return (db.query(Patient.user_id).join(User, User.id == Patient.user_id)
            .filter(Patient.id == booking.patient_id, User.is_active.is_(True)).scalar())  # an erased account gets nothing


def compose(db: Session, event: BookingEvent, booking: Booking) -> Optional[Content]:
    """The notice for ``event``, or None when it no longer applies to the booking as it is now."""
    name, status = event.event, booking.status
    if name == "payment_received":
        if booking.payment_status != PaymentStatus.PAID:
            return None
        return Content("booking_confirmed", "Booking confirmed",
                       "Payment received. We're finding a caregiver for your visit.")
    if name in ("caregiver_assigned", "caregiver_reassigned", "caregiver_en_route", "caregiver_arrived"):
        caregiver = _caregiver_for(db, event, booking)
        if caregiver is None or status not in _HAS_CAREGIVER:
            return None
        first = _first_name(caregiver.full_name)
        if name == "caregiver_assigned":
            return Content("caregiver_found", "Caregiver found", f"{first} will be your caregiver.")
        if name == "caregiver_reassigned":
            return Content("caregiver_changed", "Caregiver changed", f"{first} will now be your caregiver.")
        if name == "caregiver_en_route":
            if status != BookingStatus.EN_ROUTE:
                return None  # they have arrived since: "on the way" would be old news
            return Content("caregiver_on_the_way", "Caregiver on the way", f"{first} is on the way.")
        if status == BookingStatus.IN_PROGRESS:
            return None  # the visit has started: the code was already given
        return Content("caregiver_arrived", "Your caregiver has arrived",
                       "Open the app to see your visit code.")  # the words, never the code
    if name == "visit_started":
        if status not in (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED):
            return None
        return Content("visit_started", "Visit started", "Your visit has started.")
    if name == "visit_completed":
        if status != BookingStatus.COMPLETED:
            return None
        return Content("visit_completed", "Visit completed", "Your visit is complete.")
    if name in ("booking_cancelled_by_caregiver", "booking_cancelled_by_admin"):
        if status != BookingStatus.CANCELLED or booking.payment_status in (PaymentStatus.PENDING, PaymentStatus.FAILED):
            return None  # nothing was paid, so there is no visit they were counting on
        who = "Your caregiver can no longer attend" if name == "booking_cancelled_by_caregiver" else "Oakly has cancelled this visit"
        return Content("booking_cancelled", "Booking cancelled", f"{who}. Open the app for details.")
    if name == "no_caregiver_found":
        if booking.caregiver_id is not None or status != BookingStatus.PENDING:
            return None  # someone took it, or it is over, since
        return Content("no_caregiver_found", "No caregiver found yet",
                       "We couldn't find a caregiver yet. Our team is looking into it.")
    if name in ("refund_flagged", "payment_refund_pending", "refund_processed", "refund_needs_bank_details"):
        if name == "payment_refund_pending" and not booking.refund_required:
            return None
        refund = _refund_for(db, event)
        if refund is None:  # the older, flag-based notice: the booking carries the facts
            if name == "refund_flagged" and not booking.refund_required:
                return None
        elif refund.status in _STALE_FOR.get(name, ()):
            return None  # the refund has moved on to a state that makes this untrue
        if name in ("refund_flagged", "payment_refund_pending"):
            return Content("refund_started", "Refund started", "We've started your refund.")
        if name == "refund_processed":
            return Content("refund_completed", "Refund complete", "Your refund has been sent.")
        return Content("refund_needs_bank_details", "Bank details needed",
                       "We need your bank details to send your refund. Our team will contact you.")
    raise ValueError(f"not an app booker event: {name}")


def _write_feed_row(db: Session, user_id: uuid.UUID, booking: Booking, event: BookingEvent,
                    content: Content) -> uuid.UUID:
    """Insert the feed row (once per outbox event) and commit it. Returns its id."""
    inserted = db.execute(
        insert(Notification)
        .values(id=uuid.uuid4(), user_id=user_id, type=content.type, title=content.title, body=content.body,
                data={"booking_id": str(booking.id), "target": TARGET_BOOKING},
                created_at=event.created_at or datetime.utcnow(), event_id=event.id)
        .on_conflict_do_nothing(constraint="uq_notifications_event_id").returning(Notification.id)
    ).first()
    if inserted is None:  # a retry after a crash between this commit and the push: the row is already there
        existing = db.query(Notification.id).filter(Notification.event_id == event.id).scalar()
        db.commit()
        return existing
    db.commit()
    return inserted[0]


def deliver(db: Session, event: BookingEvent, booking: Booking) -> str:
    """Deliver one app event. Returns "sent" (a push was accepted), "no_device" (the feed row is written, nothing to
    push to or Expo said no), "stale" (it no longer applies) or "feed_failed" (nothing written: the caller retries).
    Never raises."""
    name, code = event.event, booking.booking_code  # read now: after a rollback they would be reloaded from the database
    try:
        user_id = recipient_user_id(db, booking)
        if user_id is None:
            return "stale"
        content = compose(db, event, booking)
        if content is None:
            return "stale"
        try:
            notification_id = _write_feed_row(db, user_id, booking, event, content)
        except Exception:
            db.rollback()
            logger.exception("app notice %s for booking %s: feed row not written", name, code)
            return "feed_failed"
        data: Dict[str, Any] = {"type": content.type, "booking_id": str(booking.id), "target": TARGET_BOOKING,
                                "notification_id": str(notification_id)}
        accepted = push_service.get_push_service().send_to_users(
            db, [user_id], title=content.title, body=content.body, data=data)
        return "sent" if accepted else "no_device"
    except Exception:
        db.rollback()
        logger.exception("app notice %s for booking %s could not be delivered", name, code)
        return "feed_failed"
