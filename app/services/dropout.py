"""A caregiver drops out of a WhatsApp booking: find a replacement automatically, and give the family a choice when
there is not time (owner decision 2026-10-06; before this a drop-out simply refunded, see the W3 SOP).

The flow, by ``bookings.dropout_state``:

```
caregiver cancels (accepted or on the way, before the visit started), WhatsApp booking, paid
  |
  |-- more than ``dropout_redispatch_min_lead_minutes`` before the start, and not yet on the way
  |      -> the booking is reopened (pending, no caregiver, new visit code on the next accept) and offered again by the
  |         normal dispatch, never to the caregiver who dropped. The booker is told "finding you another one".
  |      state ``redispatching``
  |         |-- a caregiver accepts (the normal accept path)            -> state cleared; the usual assigned/code messages
  |         '-- nobody by ``dropout_decision_lead_minutes`` before start -> ``awaiting_choice`` (worker, ``tick``)
  |
  '-- closer than that, or the caregiver was already on the way
         -> reopened and re-offered the same way, and straight to ``awaiting_choice``

awaiting_choice: the booker gets three buttons, stateless like W3's ``bk:*`` (every tap names the booking and is
re-checked: ownership, state, under the booking lock):
   Keep waiting     -> ``waiting``: dispatch carries on (no escalation) until the start time
   Pick another time-> ``handed_over``: a support ticket, the chat goes to a person (rescheduling is not built)
   Full refund      -> the booking is cancelled, ``refund_required`` with the whole amount paid, ``closed``
at the start time with nobody assigned (``waiting``, or an unanswered ``awaiting_choice``) -> ``handed_over``.
```

The family never pays a fee on a drop-out: the only refund here is the full amount. Every drop-out of an accepted visit
(any channel) is recorded in ``caregiver_dropouts`` so the admin console can show a caregiver's record; only WhatsApp
bookings are reopened, an app booking is cancelled exactly as before.

Locking: the caller holds the booking row lock (``BookingService.handle_booking_action``, ``choose``) and ``tick`` takes
conversation -> booking, the order everything else uses. Nothing here makes an HTTP call; the job-offer pushes are
returned to the caller to send after its commit.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.booking import Booking, BookingDispatchWave, BookingNotification, BookingStatus, PaymentStatus
from app.db.models.dropout import CaregiverDropout
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation
from app.services import booking_policy, dispatch
from app.services.booking_cancellation import flag_refund
from app.services.notification_service import NotificationService
from app.services.support import SupportService

logger = logging.getLogger(__name__)

REDISPATCHING = "redispatching"
AWAITING_CHOICE = "awaiting_choice"
WAITING = "waiting"
HANDED_OVER = "handed_over"
CLOSED = "closed"
OPEN_STATES = (REDISPATCHING, AWAITING_CHOICE, WAITING)
CHOICES = ("wait", "resched", "refund")
DROPOUT_REFUND_REASON = "caregiver_cancelled"


def dropout_count(db: Session, caregiver_id: uuid.UUID) -> int:
    return db.query(func.count(CaregiverDropout.id)).filter(CaregiverDropout.caregiver_id == caregiver_id).scalar() or 0


def has_dropped(db: Session, booking_id: uuid.UUID, caregiver_id: uuid.UUID) -> bool:
    return db.query(CaregiverDropout.id).filter(
        CaregiverDropout.booking_id == booking_id, CaregiverDropout.caregiver_id == caregiver_id).first() is not None


def dropped_ids(db: Session, booking_id: uuid.UUID) -> set:
    return {row[0] for row in db.query(CaregiverDropout.caregiver_id).filter(CaregiverDropout.booking_id == booking_id)}


def clear_on_assignment(booking: Booking) -> None:
    """A caregiver now holds the booking (their own accept, or an admin's assign): no drop-out is in progress."""
    booking.dropout_state = None


# --- the caregiver cancels -------------------------------------------------------------------------------------
def on_caregiver_cancel(db: Session, booking: Booking, caregiver_id: uuid.UUID, previous: BookingStatus, now: datetime,
                        offers: List[Dict[str, Any]]) -> bool:
    """Record the drop-out and, for a paid WhatsApp booking, reopen it for a replacement. Returns True when the booking
    was reopened (the caller must NOT cancel it) and False when the caller cancels it as it always did.

    Called with the booking row locked, inside the caller's transaction. ``offers`` collects the job-offer pushes the
    caller sends after it commits."""
    minutes = int((booking.scheduled_date - now).total_seconds() // 60)
    en_route = previous == BookingStatus.EN_ROUTE
    row = CaregiverDropout(caregiver_id=caregiver_id, booking_id=booking.id, dropped_at=now,
                           minutes_before_start=minutes, was_en_route=en_route)
    db.add(row)
    db.flush()
    if booking.channel != "whatsapp" or booking.payment_status != PaymentStatus.PAID:
        return False

    policy = booking_policy.current(db)
    booking.status = BookingStatus.PENDING
    booking.caregiver_id = None
    booking.cancel_reason = None
    booking.visit_code_enc = None
    booking.visit_code_failed_attempts = 0
    booking.visit_code_locked_until = None
    booking.on_my_way_at = None
    booking.on_my_way_distance_m = None
    booking.no_show_flagged_at = None
    booking.no_show_flagged_by = None
    booking.dispatch_state = dispatch.DISPATCHING
    booking.dispatch_started_at = now
    latest = db.query(func.max(BookingDispatchWave.wave_no)).filter(BookingDispatchWave.booking_id == booking.id).scalar() or 0
    # Offer to everyone eligible again (an earlier "no" may have been a busy hour), never to those who dropped.
    dispatch.send_wave(db, booking, latest + 1, now, offers, renotify=True)

    notifier = NotificationService(db)
    if not en_route and minutes > policy.dropout_redispatch_min_lead_minutes:
        booking.dropout_state = REDISPATCHING
        notifier.dropout_replacing(booking, str(row.id))
    else:
        booking.dropout_state = AWAITING_CHOICE
        notifier.dropout_choice_needed(booking, str(row.id))
    logger.info("booking %s: caregiver dropped out %d min before the start (en route: %s) -> %s",
                booking.booking_code, minutes, en_route, booking.dropout_state)
    return True


# --- the family's choice ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ChoiceOutcome:
    status: str                      # done | stale | not_booker | missing
    action: Optional[str] = None
    refund_kobo: int = 0
    booking_code: str = ""


def _close_open_offers(db: Session, booking: Booking, now: datetime) -> None:
    """The caregiver app lists unanswered offers as new requests: close them when the booking leaves automatic dispatch."""
    for offer in db.query(BookingNotification).filter(BookingNotification.booking_id == booking.id,
                                                       BookingNotification.response.is_(None)):
        offer.response = "booking_cancelled"
        offer.response_at = now


def choose(db: Session, booking_id: uuid.UUID, user_id: uuid.UUID, action: str, now: Optional[datetime] = None, *,
           contact_id: Optional[uuid.UUID] = None, commit: bool = True) -> ChoiceOutcome:
    """Apply the booker's tap. Everything is decided under the booking lock: the booker, the state, that nobody holds
    the booking. ``commit=False`` is for the chat step, which owns the transaction (and holds the conversation lock)."""
    now = now or datetime.utcnow()

    def release() -> None:
        if commit:
            db.rollback()

    if action not in CHOICES:
        raise ValueError(f"unknown drop-out choice {action!r}")
    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().populate_existing().first()
    if booking is None:
        release()
        return ChoiceOutcome("missing")
    code = booking.booking_code
    if booking.booked_by_user_id is None or booking.booked_by_user_id != user_id:
        release()
        return ChoiceOutcome("not_booker", booking_code=code)
    if (booking.dropout_state != AWAITING_CHOICE or booking.status != BookingStatus.PENDING
            or booking.caregiver_id is not None or booking.payment_status != PaymentStatus.PAID):
        release()
        return ChoiceOutcome("stale", action, booking_code=code)

    refund = 0
    if action == "wait":
        booking.dropout_state = WAITING
        booking.dispatch_state = dispatch.DISPATCHING  # dispatch carries on; it is not escalated before the start
        booking.dispatch_started_at = now
    elif action == "resched":
        booking.dropout_state = HANDED_OVER
        booking.dispatch_state = dispatch.UNMATCHED  # shows in the admin unmatched queue
        _close_open_offers(db, booking, now)  # a person looks after it now; an admin can offer or assign again
        if contact_id is None:
            contact_id = db.query(WhatsAppContact.id).filter(WhatsAppContact.user_id == user_id).scalar()
        if contact_id is not None:
            SupportService(db).open(contact_id=contact_id, reason="talk_to_person", priority="normal")
    else:  # refund: the whole amount paid, never a fee
        refund = int(booking.total_amount_kobo)
        booking.status = BookingStatus.CANCELLED
        booking.cancel_reason = DROPOUT_REFUND_REASON
        booking.dropout_state = CLOSED
        flag_refund(booking, DROPOUT_REFUND_REASON, refund)
        _close_open_offers(db, booking, now)
    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("booking %s: drop-out choice %s", code, action)
    return ChoiceOutcome("done", action, refund, code)


# --- the worker: time-driven decision points ---------------------------------------------------------------------------
def _contact_for(db: Session, booking: Booking) -> Optional[WhatsAppContact]:
    if booking.booked_by_user_id is None:
        return None
    return db.query(WhatsAppContact).filter(WhatsAppContact.user_id == booking.booked_by_user_id).first()


def _hand_over(db: Session, booking: Booking, contact: Optional[WhatsAppContact],
               conversation: Optional[WhatsAppConversation], now: datetime) -> None:
    from app.channels.whatsapp.engine import State  # local: the channel layer imports services, not the reverse

    booking.dropout_state = HANDED_OVER
    booking.dispatch_state = dispatch.UNMATCHED
    _close_open_offers(db, booking, now)
    last = db.query(CaregiverDropout.id).filter(CaregiverDropout.booking_id == booking.id).order_by(
        CaregiverDropout.dropped_at.desc()).first()
    NotificationService(db).dropout_handed_over(booking, str(last[0]) if last else "")
    if contact is not None:
        SupportService(db).open(contact_id=contact.id, reason="talk_to_person", priority="normal")
    if conversation is not None:
        conversation.state = State.HUMAN.value
        conversation.automation_paused = True
        conversation.updated_at = now
    logger.warning("booking %s: nobody found by the start time; handed to a person", booking.booking_code)


def tick(db: Session, now: Optional[datetime] = None) -> int:
    """Move every booking with a drop-out in progress along. Returns how many changed. Safe with several workers
    (locks are skip-locked and every step re-checks under them)."""
    now = now or datetime.utcnow()
    pending = [(row.id, row.booked_by_user_id) for row in db.query(Booking.id, Booking.booked_by_user_id).filter(
        Booking.dropout_state.in_(OPEN_STATES))]
    db.rollback()
    changed = 0
    for booking_id, user_id in pending:
        try:
            contact = db.query(WhatsAppContact).filter(WhatsAppContact.user_id == user_id).first() if user_id else None
            conversation = None
            if contact is not None:  # conversation first, then the booking: the order the chat step takes them in
                conversation = (db.query(WhatsAppConversation).filter(WhatsAppConversation.contact_id == contact.id)
                                .with_for_update(skip_locked=True).populate_existing().first())
                if conversation is None:
                    db.rollback()
                    continue  # the person is mid-step in the chat; the next tick will do it
            booking = (db.query(Booking).filter(Booking.id == booking_id).with_for_update(skip_locked=True)
                       .populate_existing().first())
            if booking is None:
                db.rollback()
                continue
            if _advance(db, booking, contact, conversation, now):
                changed += 1
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("drop-out tick failed for booking %s", booking_id)
    return changed


def _advance(db: Session, booking: Booking, contact: Optional[WhatsAppContact],
             conversation: Optional[WhatsAppConversation], now: datetime) -> bool:
    state = booking.dropout_state
    if state not in OPEN_STATES:
        return False
    if booking.status != BookingStatus.PENDING or booking.caregiver_id is not None:
        # Someone holds it, or it was cancelled (an admin): nothing left for this machine to do.
        booking.dropout_state = None if booking.caregiver_id is not None else CLOSED
        return False
    start = booking.scheduled_date
    if state == REDISPATCHING:
        lead = timedelta(minutes=booking_policy.current(db).dropout_decision_lead_minutes)
        if now < start - lead:
            return False
        booking.dropout_state = AWAITING_CHOICE
        booking.dispatch_started_at = now  # the usual 30 minutes of looking apply again from the moment they are asked
        last = db.query(CaregiverDropout.id).filter(CaregiverDropout.booking_id == booking.id).order_by(
            CaregiverDropout.dropped_at.desc()).first()
        NotificationService(db).dropout_choice_needed(booking, str(last[0]) if last else "")
        return True
    if now >= start:  # waiting, or a choice nobody answered: the visit time has come with nobody assigned
        _hand_over(db, booking, contact, conversation, now)
        return True
    return False
