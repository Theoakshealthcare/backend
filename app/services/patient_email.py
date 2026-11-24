"""E-mailing an app patient about their booking: the same events that write the feed row and push
(``app.services.patient_notifications``), for the ones worth an inbox: booking confirmed + payment receipt, caregiver
found or changed, visit completed, booking cancelled, no caregiver found, refund sent. Not e-mailed (push and feed only,
too noisy): the caregiver is on the way, the visit has started, the caregiver has arrived, "refund started", "bank
details needed".

Transactional notices only (receipts and status): no marketing, no unsubscribe link.

Rules this module keeps:
- **Best effort, after the commit.** ``deliver`` calls ``send_for_event`` once the feed row is committed. It never raises
  and never rolls back anything it did not do itself; a mail that fails is logged by event kind and dropped.
- **Once.** The caller only asks for a mail on the pass that inserted the feed row (``notifications.event_id`` is unique),
  so a replayed outbox event sends nothing. The cost: a crash between the commit and the send loses that one mail.
- **Only for the booker, only for an app booking, only when they have an e-mail address.**
- **Nothing sensitive.** The only inputs are the booker's and the caregiver's first names, the service, the visit time,
  the booking code, and money and the payment reference as stored. Never the address, phone numbers, ID data, the
  condition, special instructions, the visit code or anything from the care plan or the visit notes.
- **Money is read, never computed.** Amounts come straight from the booking (``total_amount_kobo``, ``refund_amount_kobo``)
  or the refund row (``amount_kobo``); the same fallbacks as the WhatsApp notices (``refund_amount_kobo``).
- **Logs name the event kind only**, never an address or a body.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_rules as rules
from app.channels.whatsapp.booking_notices import _caregiver_for, _first_name, _refund_for, refund_amount_kobo
from app.core.config import settings
from app.db.models.booking import Booking, PaymentStatus
from app.db.models.refund import REFUND_DEST_WALLET
from app.db.models.user import User
from app.db.models.whatsapp_booking import BookingEvent
from app.services import email as email_service

logger = logging.getLogger(__name__)

# The events that get an e-mail. Everything else the feed carries is push-only.
EMAILED_EVENTS = ("payment_received", "caregiver_assigned", "caregiver_reassigned", "visit_completed",
                  "booking_cancelled_by_caregiver", "booking_cancelled_by_admin", "no_caregiver_found",
                  "refund_processed")

CURRENCY = "Nigerian naira (NGN)"  # bookings carry no currency column: every amount is naira, in kobo


@dataclass(frozen=True)
class Mail:
    subject: str
    heading: str
    paragraphs: Tuple[str, ...]
    facts: Tuple[Tuple[str, str], ...] = ()


def _service(booking: Booking) -> str:
    return rules.SERVICE_LABELS.get(booking.service_type, str(booking.service_type).replace("_", " ").title())


def _duration(minutes: int) -> str:
    hours, rest = divmod(int(minutes), 60)
    parts = ([f"{hours} hour" + ("" if hours == 1 else "s")] if hours else []) + ([f"{rest} minutes"] if rest else [])
    return " ".join(parts) or "0 minutes"


def _when(booking: Booking) -> str:
    """Sat 4 Oct 2026, 10:00-14:00 (Lagos time)."""
    start, end = rules.to_lagos(booking.scheduled_date), rules.to_lagos(booking.scheduled_end_date)
    return f"{start:%a} {start.day} {start:%b %Y}, {start:%H:%M}-{end:%H:%M} (Lagos time)"


def _refund_sentence(booking: Booking) -> Optional[str]:
    """One line about a refund after a cancellation, or None when none follows."""
    if not booking.refund_required:
        return None
    if (booking.payment_status == PaymentStatus.PAID and booking.refund_amount_kobo is None
            and booking.actual_start_time is not None):
        return "The visit had started, so our team will contact you about any refund."  # no automatic amount
    return (f"We've started a refund of {rules.naira(refund_amount_kobo(booking))}. "
            "We'll e-mail you again when it has been sent.")


def _refund_destination(destination: Optional[str]) -> str:
    if destination == REFUND_DEST_WALLET:
        return "It has been added to your Oakly wallet."
    return "It goes back to the card or bank account you paid with. Your bank may take a few days to show it."


def compose_mail(db: Session, event: BookingEvent, booking: Booking) -> Optional[Mail]:
    """The e-mail for ``event``, or None when this event is not e-mailed. The caller has already checked, through
    ``patient_notifications.compose``, that the event still applies to the booking."""
    name = event.event
    if name == "payment_received":
        facts: List[Tuple[str, str]] = [("Service", _service(booking)), ("Date and time", _when(booking)),
                                        ("Duration", _duration(booking.duration_minutes)),
                                        ("Amount paid", rules.naira(booking.total_amount_kobo)),
                                        ("Currency", CURRENCY)]
        if booking.paystack_reference:
            facts.append(("Payment reference", booking.paystack_reference))
        return Mail("Booking confirmed: payment received", "Booking confirmed",
                    ("Payment received. We're finding a caregiver for your visit. We'll tell you as soon as "
                     "one is assigned.", "Keep this e-mail as your receipt."), tuple(facts))
    if name in ("caregiver_assigned", "caregiver_reassigned"):
        caregiver = _caregiver_for(db, event, booking)
        if caregiver is None:
            return None
        first = _first_name(caregiver.full_name)
        if name == "caregiver_assigned":
            return Mail("Your caregiver has been assigned", "Caregiver found",
                        (f"{first} will be your caregiver.",), (("Visit", _when(booking)),))
        return Mail("Your caregiver has changed", "Caregiver changed",
                    (f"{first} will now be your caregiver.",), (("Visit", _when(booking)),))
    if name == "visit_completed":
        return Mail("Your visit is complete", "Visit completed",
                    ("Your visit is complete. Thank you for choosing us.",),
                    (("Service", _service(booking)), ("Date", _when(booking))))
    if name in ("booking_cancelled_by_caregiver", "booking_cancelled_by_admin"):
        who = ("Your caregiver can no longer attend, so this visit has been cancelled."
               if name == "booking_cancelled_by_caregiver" else "Oakly has cancelled this visit.")
        refund = _refund_sentence(booking)
        return Mail("Your booking was cancelled", "Booking cancelled",
                    (who,) + ((refund,) if refund else ()) + ("Open the app for details.",),
                    (("Service", _service(booking)), ("Visit was for", _when(booking))))
    if name == "no_caregiver_found":
        refund = _refund_sentence(booking)
        return Mail("We're still looking for a caregiver", "No caregiver found yet",
                    ("We couldn't find a caregiver yet. Our team is looking into it.",) + ((refund,) if refund else ()),
                    (("Service", _service(booking)), ("Requested for", _when(booking))))
    if name == "refund_processed":
        refund = _refund_for(db, event)
        amount = refund.amount_kobo if refund is not None and refund.amount_kobo is not None \
            else refund_amount_kobo(booking)
        destination = refund.destination if refund is not None else None
        return Mail("Your refund has been sent", "Refund sent",
                    ("Your refund has been sent.", _refund_destination(destination)),
                    (("Refund amount", rules.naira(amount)), ("Currency", CURRENCY)))
    return None


def send_for_event(db: Session, user_id, event: BookingEvent, booking: Booking, name: str, code: str) -> bool:
    """Send the e-mail for one delivered event. ``name`` and ``code`` are the event kind and booking code, read before
    the commit. Returns True when the transport accepted it. Never raises."""
    if name not in EMAILED_EVENTS:
        return False
    try:
        if booking.channel != "app":
            return False  # a WhatsApp booker is served on WhatsApp
        user = db.query(User.email, User.full_name).filter(User.id == user_id).first()
        if user is None or not (user.email or "").strip():
            return False
        mail = compose_mail(db, event, booking)
        if mail is None:
            return False
        html = email_service.render_template("patient_booking", {
            "project_name": settings.PROJECT_NAME, "heading": mail.heading, "first_name": _first_name_of(user.full_name),
            "paragraphs": mail.paragraphs, "facts": mail.facts, "booking_code": code,
        })
        sent = email_service.send_email(user.email.strip(), f"{settings.PROJECT_NAME} - {mail.subject}", html)
        logger.info("patient e-mail %s %s", name, "sent" if sent else "not sent")
        return sent
    except Exception:
        try:
            db.rollback()
        except Exception:  # the session is unusable: nothing more to do for a courtesy mail
            pass
        logger.exception("patient e-mail %s failed", name)  # the kind only: no address, no body
        return False


def _first_name_of(full_name: Optional[str]) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else "there"
