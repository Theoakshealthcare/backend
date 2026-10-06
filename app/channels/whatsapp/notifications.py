"""Telling the person what happened to their booking (W2: the payment messages), from the ``booking_events`` outbox.

Domain code records an event in the same transaction as the change (``app.services.booking_events.emit``); this
module delivers it, from the worker (every 15 seconds) and, for the person who just came back to the chat, on
demand. Two properties matter:

- Exactly one message per event. ``(booking_id, event)`` is unique, a worker *claims* an event with a
  compare-and-set (``claimed_at``) before sending, and the send is never repeated once ``delivered_at`` is set.
  A claim whose worker died is retried after two minutes, up to five attempts; Meta rejecting a message for good
  (a blocked number, no WhatsApp account) stops the retries when the attempts run out.
- The 24-hour rule is respected by construction: inside the window the same words go out as free text (with the
  Pay now / Book again buttons); outside it the approved template does (``templates.choose``). A person who sent
  STOP gets only the *essential* messages (a payment they made).

It also does the time-driven part of the payment journey: the reminder 20 minutes after the link was sent
(WA-FR-24; the cancellation at 30 minutes is the existing expiry job, which emits its own event) and the sweep that
removes drafts nobody finished.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_chat, booking_rules as rules
from app.channels.whatsapp.meta_client import MetaCloudClient
from app.channels.whatsapp.outbound import send_and_record
from app.channels.whatsapp.templates import TEMPLATES, choose, within_window
from app.core.config import settings
from app.core.phone import contact_ref
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation
from app.db.models.whatsapp_booking import OPEN_QUOTE_STATUSES, BookingEvent, Quote
from app.services import booking_events

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
CLAIM_LEASE = timedelta(minutes=2)
TERMINAL_EVENTS = ("payment_received", "payment_refund_pending", "booking_cancelled_unpaid")  # after these the chat is no longer "paying"


# --- time-driven ------------------------------------------------------------------------------------
def scan_reminders(db: Session, now: datetime) -> int:
    """Queue ``payment_link_expiring`` for unpaid WhatsApp bookings whose link has been out for
    WHATSAPP_PAYMENT_REMINDER_MINUTES (and still has time left). One per booking, whatever the retries."""
    lead = timedelta(minutes=max(0, settings.PAYMENT_LINK_TTL_MINUTES - settings.WHATSAPP_PAYMENT_REMINDER_MINUTES))
    due = (db.query(Booking)
           .join(Quote, Quote.booking_id == Booking.id)
           .filter(Booking.channel == "whatsapp", Booking.status == BookingStatus.PENDING,
                   Booking.payment_status == PaymentStatus.PENDING,
                   Booking.payment_expires_at.isnot(None), Booking.payment_expires_at > now,
                   Booking.payment_expires_at - lead <= now,
                   Quote.payload["payment_url"].astext.isnot(None))  # no link, no reminder
           .all())
    queued = sum(1 for booking in due if booking_events.emit(db, booking, "payment_link_expiring"))
    db.commit()
    return queued


def sweep_stale(db: Session, now: datetime) -> int:
    """Close drafts and quotes nobody finished (their encrypted answers go with them), wipe the answers of
    confirmed quotes whose payment link never materialised, and reset conversations stuck in a booking state."""
    cutoff = now - timedelta(minutes=2 * settings.WHATSAPP_IDLE_EXPIRY_MINUTES)
    stale = (db.query(Quote).filter(Quote.status.in_(OPEN_QUOTE_STATUSES), Quote.updated_at < cutoff)
             .with_for_update(skip_locked=True).all())
    for quote in stale:
        booking_chat.close_quote(quote, "expired")
    leftovers = (db.query(Quote).filter(Quote.status == "confirmed", Quote.sensitive_encrypted.isnot(None),
                                        Quote.updated_at < now - timedelta(hours=1))
                 .with_for_update(skip_locked=True).all())
    for quote in leftovers:
        quote.sensitive_encrypted = None
    stuck = (db.query(WhatsAppConversation)
             .filter(WhatsAppConversation.state.in_([s.value for s in booking_chat.EXPIRABLE_STATES]),
                     WhatsAppConversation.updated_at < cutoff)
             .with_for_update(skip_locked=True).all())
    for conversation in stuck:
        conversation.state = "idle"
        conversation.context = {k: v for k, v in (conversation.context or {}).items()
                                if k not in ("quote_id", "booking_id", "waitlist_quote_id")}
    db.commit()
    return len(stale) + len(leftovers) + len(stuck)


# --- delivery -----------------------------------------------------------------------------------
def _claim(db: Session, event_id, now: datetime) -> bool:
    """Compare-and-set: exactly one caller gets True for an undelivered event with attempts left."""
    claimed = db.execute(
        update(BookingEvent)
        .where(BookingEvent.id == event_id, BookingEvent.delivered_at.is_(None), BookingEvent.attempts < MAX_ATTEMPTS,
               or_(BookingEvent.claimed_at.is_(None), BookingEvent.claimed_at < now - CLAIM_LEASE))
        .values(claimed_at=now, attempts=BookingEvent.attempts + 1)
        .returning(BookingEvent.id)
    ).first()
    db.commit()
    return claimed is not None


def _finish(db: Session, event_id, now: datetime, error: Optional[str]) -> None:
    values: Dict[str, object] = {"last_error": error}
    if error is None or error in ("no_contact", "opted_out", "stale"):
        values["delivered_at"] = now  # sent, or there was nothing (more) to send
    db.execute(update(BookingEvent).where(BookingEvent.id == event_id).values(**values))
    db.commit()


def _contact_for(db: Session, booking: Booking) -> Optional[WhatsAppContact]:
    if booking.booked_by_user_id is None:
        return None
    return db.query(WhatsAppContact).filter(WhatsAppContact.user_id == booking.booked_by_user_id).first()


def _reset_conversation(db: Session, contact: WhatsAppContact, booking: Booking) -> None:
    conversation = (db.query(WhatsAppConversation).filter_by(contact_id=contact.id).populate_existing()
                    .with_for_update().first())
    if conversation is not None and conversation.state == "awaiting_payment" \
            and (conversation.context or {}).get("booking_id") == str(booking.id):
        conversation.state = "idle"
        conversation.context = {k: v for k, v in (conversation.context or {}).items()
                                if k not in ("quote_id", "booking_id")}
        conversation.updated_at = datetime.utcnow()
    db.commit()


def _message_for(db: Session, event: BookingEvent, booking: Booking, contact: WhatsAppContact, now: datetime):
    """The message to send, or None with the reason it should not be sent (``stale``)."""
    name = event.event
    in_window = within_window(contact.last_inbound_at, now)
    params = {"booking_code": booking.booking_code}
    if name == "payment_link_expiring":  # the wording follows the settings, never a number baked into the copy
        params["minutes"] = str(max(0, settings.PAYMENT_LINK_TTL_MINUTES - settings.WHATSAPP_PAYMENT_REMINDER_MINUTES))
    url: Optional[str] = None
    if name == "payment_received":
        if booking.payment_status != PaymentStatus.PAID:
            return None, "stale"
        params["total"] = rules.naira(booking.total_amount_kobo)
    elif name == "payment_link_expiring":
        if booking.payment_status != PaymentStatus.PENDING or booking.status != BookingStatus.PENDING:
            return None, "stale"
        quote = db.query(Quote).filter(Quote.booking_id == booking.id).first()
        url = (quote.payload or {}).get("payment_url") if quote else None
        if not url:
            return None, "stale"
    elif name == "payment_refund_pending":
        if not booking.refund_required:
            return None, "stale"
        params["total"] = rules.naira(booking.total_amount_kobo)
    elif name == "booking_cancelled_unpaid":
        if booking.payment_status == PaymentStatus.PAID:  # paid after the cancel and reinstated: say nothing
            return None, "stale"
    return choose(name, params, in_window=in_window, url=url), None


def deliver_pending(db: Session, *, booking_id=None, now: Optional[datetime] = None,
                    client: Optional[MetaCloudClient] = None, limit: int = 50) -> int:
    """Send the events that are due. ``booking_id`` limits it to one booking (the person is in the chat now)."""
    now = now or datetime.utcnow()
    query = db.query(BookingEvent.id).filter(
        BookingEvent.delivered_at.is_(None), BookingEvent.attempts < MAX_ATTEMPTS,
        or_(BookingEvent.claimed_at.is_(None), BookingEvent.claimed_at < now - CLAIM_LEASE))
    if booking_id is not None:
        query = query.filter(BookingEvent.booking_id == booking_id)
    event_ids: List = [row.id for row in query.order_by(BookingEvent.created_at).limit(limit)]
    db.rollback()
    sent = 0
    for event_id in event_ids:
        try:
            if not _claim(db, event_id, now):
                continue
            client = client or MetaCloudClient.from_settings()
            sent += 1 if _deliver_one(db, event_id, now, client) else 0
        except Exception:
            db.rollback()
            logger.exception("whatsapp booking event %s could not be delivered", event_id)
    return sent


def _deliver_one(db: Session, event_id, now: datetime, client: MetaCloudClient) -> bool:
    event = db.query(BookingEvent).filter(BookingEvent.id == event_id).populate_existing().one()
    booking = db.query(Booking).filter(Booking.id == event.booking_id).populate_existing().one()
    name, contact = event.event, _contact_for(db, booking)
    if contact is None or contact.blocked or booking.channel != "whatsapp":
        _finish(db, event_id, now, "no_contact")
        return False
    if contact.opted_out_at is not None and not TEMPLATES[name].essential:
        _finish(db, event_id, now, "opted_out")  # STOP means no non-essential messages (WA-FR-8)
        if name in TERMINAL_EVENTS:
            _reset_conversation(db, contact, booking)
        return False
    if name == "booking_cancelled_unpaid" and booking.payment_status != PaymentStatus.PAID:
        # Do not announce a cancellation for a booking that was paid on the link whose webhook never came.
        from app.services.booking_payment import verify_and_settle
        verify_and_settle(db, booking.id)
        db.expire_all()
        booking = db.query(Booking).filter(Booking.id == booking.id).populate_existing().one()
        event = db.query(BookingEvent).filter(BookingEvent.id == event_id).populate_existing().one()
    message, reason = _message_for(db, event, booking, contact, now)
    if message is None:
        _finish(db, event_id, now, reason)
        return False
    result = send_and_record(db, client, contact, message, now=now)
    if not result.ok:
        logger.warning("whatsapp %s for booking %s to contact %s not sent: %s", name, booking.booking_code,
                       contact_ref(contact.wa_id), result.error_code)
        db.execute(update(BookingEvent).where(BookingEvent.id == event_id).values(last_error=result.error_code))
        db.commit()
        return False
    _finish(db, event_id, now, None)
    if name in TERMINAL_EVENTS:
        _reset_conversation(db, contact, booking)
    return True


def run_events(db: Session, now: Optional[datetime] = None) -> Dict[str, int]:
    """One pass of the worker job: queue due reminders, deliver what is waiting, sweep stale drafts."""
    now = now or datetime.utcnow()
    counts = {"reminders": 0, "delivered": 0, "swept": 0}
    for key, step in (("reminders", scan_reminders), ("delivered", lambda d, n: deliver_pending(d, now=n)),
                      ("swept", sweep_stale)):
        try:
            counts[key] = step(db, now)
        except Exception:
            db.rollback()
            logger.exception("whatsapp events: %s failed", key)
    return counts
