"""What the booker is told for the visit's journey (W3): the WhatsApp wording for each outbox event.

``build`` is called by the delivery worker (``notifications._deliver_one``) with the event, the booking *as it is
now* and the contact, and returns the message to send or ``(None, "stale")`` when the event no longer applies (a
caregiver who has since been replaced, a visit that is not under way any more). Nothing is stored in the outbox
beyond ``(booking, event, key)``, so a stale event is dropped rather than announced.

No health data (WA-NFR-6): the only inputs are a booking code, a caregiver's first name and experience, a date and
time, money, and the visit code. The condition, address and special instructions are never read here.
"""

import logging
from datetime import datetime
from typing import Optional, Tuple

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_rules as rules
from app.channels.whatsapp.messages import OutboundMessage
from app.channels.whatsapp.templates import choose
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import Caregiver
from app.db.models.whatsapp_booking import BookingEvent
from app.services.notification_service import code_fingerprint

logger = logging.getLogger(__name__)

# The event name differs from the template's for these; every other event is its template's name.
TEMPLATE_FOR = {
    "visit_code_issued": "visit_code",
    "visit_completed": "visit_completed_feedback",
    "booking_cancelled_by_caregiver": "booking_update",
    "booking_cancelled_by_admin": "booking_update",
    "caregiver_reassigned": "booking_update",
    "refund_flagged": "refund_update",
    "refund_processed": "refund_update",
}
JOURNEY_EVENTS = ("caregiver_assigned", "visit_code_issued", "caregiver_en_route", "visit_started", "visit_completed",
                  "booking_cancelled_by_caregiver", "booking_cancelled_by_admin", "caregiver_reassigned",
                  "refund_flagged", "refund_processed")

_HAS_CAREGIVER = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)
Built = Tuple[Optional[OutboundMessage], Optional[str]]


def template_for(event: str) -> str:
    return TEMPLATE_FOR.get(event, event)


def _first_name(full_name: Optional[str]) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else "Your professional"


def _experience(years: Optional[int]) -> str:
    if not years or years < 1:
        return "verified professional"
    return f"{years} year" + ("" if years == 1 else "s")


def refund_amount_kobo(booking: Booking) -> int:
    """What the refund is for: the amount the policy decided, else everything that was paid."""
    return booking.refund_amount_kobo if booking.refund_amount_kobo is not None else booking.total_amount_kobo


def _caregiver_for(db: Session, event: BookingEvent, booking: Booking) -> Optional[Caregiver]:
    """The caregiver the event was about, but only while they are still the one on the booking."""
    caregiver_part, _, issued = event.dedupe_key.partition(":")
    if not booking.caregiver_id or str(booking.caregiver_id) != caregiver_part:
        return None
    if issued and issued != code_fingerprint(booking):
        return None  # an earlier assignment of the same caregiver: a newer event carries the code that works now
    return db.query(Caregiver).filter(Caregiver.id == booking.caregiver_id).first()


def _update_sentence(event: str, booking: Booking) -> str:
    if event == "booking_cancelled_by_admin":
        return "Our team has cancelled this visit."  # the refund, if any, follows as its own refund_update message
    who = "Your professional can no longer attend, so the visit is cancelled."
    if booking.refund_required and booking.refund_amount_kobo is None and booking.payment_status == PaymentStatus.PAID \
            and booking.actual_start_time is not None:
        return "The visit ended early. Our team will contact you about any refund."  # started, so no automatic amount
    if booking.refund_required:
        return f"{who} We've started a refund of {rules.naira(refund_amount_kobo(booking))}."
    return who


def build(db: Session, event: BookingEvent, booking: Booking, now: datetime, *, in_window: bool) -> Built:
    name = event.event
    code = booking.booking_code
    status = booking.status

    if name in ("caregiver_assigned", "visit_code_issued", "caregiver_en_route", "caregiver_reassigned"):
        caregiver = _caregiver_for(db, event, booking)
        if caregiver is None or status not in _HAS_CAREGIVER:
            return None, "stale"
        if name == "caregiver_en_route" and status != BookingStatus.EN_ROUTE:
            return None, "stale"  # they have arrived since: "on the way" would be old news
        if name == "caregiver_assigned":
            start = rules.to_lagos(booking.scheduled_date)
            params = {"booking_code": code, "caregiver_first_name": _first_name(caregiver.full_name),
                      "date": f"{start:%a} {start.day} {start:%b}", "time": f"{start:%H:%M}",
                      "experience": _experience(caregiver.years_of_experience)}
        elif name == "caregiver_en_route":
            params = {"booking_code": code, "caregiver_first_name": _first_name(caregiver.full_name)}
        elif name == "caregiver_reassigned":
            start = rules.to_lagos(booking.scheduled_date)
            params = {"booking_code": code, "update": (
                f"Your professional has changed. {_first_name(caregiver.full_name)} will now visit on "
                f"{start:%a} {start.day} {start:%b} at {start:%H:%M}.")}
        else:
            if status == BookingStatus.IN_PROGRESS or not booking.visit_code_enc:
                return None, "stale"
            try:
                visit_code = decrypt(booking.visit_code_enc)
            except InvalidToken:
                logger.error("booking %s: visit code cannot be decrypted; not sent", code)  # never the code itself
                return None, "stale"
            params = {"booking_code": code, "visit_code": visit_code}
    elif name == "visit_started":
        if status not in (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED):
            return None, "stale"
        params = {"booking_code": code}
    elif name == "visit_completed":
        if status != BookingStatus.COMPLETED:
            return None, "stale"
        params = {"booking_code": code}
    elif name in ("booking_cancelled_by_caregiver", "booking_cancelled_by_admin"):
        if status != BookingStatus.CANCELLED or booking.payment_status == PaymentStatus.PENDING:
            return None, "stale"  # nothing was paid, so there is no visit they were counting on
        params = {"booking_code": code, "update": _update_sentence(name, booking)}
    elif name in ("refund_flagged", "refund_processed"):
        if name == "refund_flagged" and not booking.refund_required:
            return None, "stale"
        params = {"amount": rules.naira(refund_amount_kobo(booking)), "booking_code": code,
                  "status": "being processed" if name == "refund_flagged" else "complete"}
    else:
        raise ValueError(f"not a journey event: {name}")
    return choose(template_for(name), params, in_window=in_window), None
