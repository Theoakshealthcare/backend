"""Telling people what happened to a booking, whichever channel they are on (W3, WA-FR-30 to WA-FR-37).

Two halves, deliberately apart:

1. **Recording** (this class). Booking code calls one named method per domain change ("the caregiver accepted",
   "the visit started") *inside the transaction that makes the change*. The method writes outbox rows
   (``booking_events``) and returns. Nothing is sent inline, so a slow or failing Meta/Expo can never roll back or
   delay a booking transition, and an event exists if and only if the change committed.
2. **Delivery** (the worker, ``app.channels.whatsapp.notifications.deliver_pending``). Each row is delivered for its
   audience: a *booker* event becomes a WhatsApp message (template outside the 24 h window), a *caregiver* event
   becomes an Expo push (``deliver_caregiver_push`` below). The wording for WhatsApp lives in
   ``app.channels.whatsapp.booking_notices``; the channel-independent facts live on the booking.

What an event may carry: nothing. Rows hold only ``(booking, event, key)``; the message is built at delivery time
from the booking as it is then, so a stale event (a caregiver who has since been replaced) is dropped instead of
announced, and no health data is ever copied into the outbox (WA-NFR-6).
"""

import hashlib
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.booking import Booking
from app.db.models.caregiver import Caregiver
from app.services import booking_events
from app.services import push as push_service

logger = logging.getLogger(__name__)


def code_fingerprint(booking: Booking) -> str:
    """Identifies one *issuance* of a visit code. Fernet tokens are randomised, so every accept or reassign gives a
    new value even when the same caregiver comes back (A, then B, then A again)."""
    return hashlib.sha256((booking.visit_code_enc or "").encode()).hexdigest()[:16]


def issuance_key(booking: Booking) -> str:
    """``<caregiver id>:<code fingerprint>``: the dedupe key of the events that belong to one assignment."""
    return f"{booking.caregiver_id}:{code_fingerprint(booking)}"


class NotificationService:
    """Record the notifications a booking change calls for. Never commits and never sends."""

    def __init__(self, db: Session):
        self.db = db

    def _emit(self, booking: Booking, event: str, key: str = "") -> bool:
        return booking_events.emit(self.db, booking, event, key)

    @staticmethod
    def _caregiver_key(booking: Booking) -> str:
        return str(booking.caregiver_id) if booking.caregiver_id else ""

    @staticmethod
    def _issuance_key(booking: Booking) -> str:
        return issuance_key(booking) if booking.caregiver_id else ""

    # --- the visit's journey --------------------------------------------------------------------
    def caregiver_accepted(self, booking: Booking) -> None:
        """A caregiver took the booking: tell the booker who is coming, then the visit code (WA-FR-30, WA-FR-35).
        Keyed by the assignment (caregiver and code), so a replacement, or the same caregiver coming back after another,
        is announced with the code that works now."""
        key = self._issuance_key(booking)
        self._emit(booking, "caregiver_assigned", key)
        self._emit(booking, "visit_code_issued", key)

    def caregiver_on_the_way(self, booking: Booking) -> None:
        self._emit(booking, "caregiver_en_route", self._issuance_key(booking))

    def visit_started(self, booking: Booking) -> None:
        self._emit(booking, "visit_started")

    def visit_completed(self, booking: Booking) -> None:
        self._emit(booking, "visit_completed")

    # --- cancellations and refunds ----------------------------------------------------------------
    def cancelled_by_caregiver(self, booking: Booking) -> None:
        """The caregiver dropped out. The booker is told the next step (the booking's refund fields say which)."""
        self._emit(booking, "booking_cancelled_by_caregiver", self._caregiver_key(booking))

    def dropout_replacing(self, booking: Booking, dropout_id: str) -> None:
        """The caregiver dropped out far enough ahead that we are finding another one: tell the booker there is nothing
        to do. Keyed by the drop-out (a booking can lose more than one caregiver)."""
        self._emit(booking, "caregiver_dropped_replacing", dropout_id)

    def dropout_choice_needed(self, booking: Booking, dropout_id: str) -> None:
        """No replacement yet and little time left (or they were already on the way): give the booker the choice."""
        self._emit(booking, "dropout_choice_needed", dropout_id)

    def dropout_handed_over(self, booking: Booking, dropout_state_key: str = "") -> None:
        """Nobody was found by the start time: a person takes over."""
        self._emit(booking, "dropout_handed_over", dropout_state_key)

    def caregiver_reassigned(self, booking: Booking) -> None:
        """An admin moved the visit to another caregiver (A2 reassign). The booker hears who is coming now and gets
        the new visit code (the old one died with the old assignment). Keyed by the new assignment."""
        key = self._issuance_key(booking)
        self._emit(booking, "caregiver_reassigned", key)
        self._emit(booking, "visit_code_issued", key)

    def cancelled_by_admin(self, booking: Booking, *, caregiver_pushed: bool = False) -> None:
        """An admin cancelled. The booker is told it is cancelled; the refund is announced later, when it is actually sent
        to Paystack (refunds.dispatch_due), because nobody has approved it yet. ``caregiver_pushed``: the caller already
        pushes the caregiver itself."""
        self._emit(booking, "booking_cancelled_by_admin")
        if not caregiver_pushed:
            self._tell_caregiver_it_is_off(booking)

    def cancelled_by_booker(self, booking: Booking) -> None:
        """The booker cancelled in chat. They already have the answer on screen, so only the caregiver is told."""
        self._tell_caregiver_it_is_off(booking)

    def refund_flagged(self, booking: Booking) -> None:
        """A refund was set in motion for a booking (WA-FR-31/37, Admin PRD refund module)."""
        self._emit(booking, "refund_flagged")

    def refund_processed(self, booking: Booking) -> None:
        self._emit(booking, "refund_processed")

    def _tell_caregiver_it_is_off(self, booking: Booking) -> None:
        if booking.caregiver_id is not None:
            self._emit(booking, "caregiver_booking_cancelled", self._caregiver_key(booking))


# --- caregiver delivery (Expo push) ----------------------------------------------------------------
def deliver_caregiver_push(db: Session, event: str, booking: Booking, caregiver_id: Optional[str]) -> str:
    """Push a caregiver event. Returns "sent", "no_device" (nothing to push to) or "stale" (the event no longer
    applies). Best effort like every push: the app also polls, so a failed push is not retried and never blocks."""
    if event != "caregiver_booking_cancelled":
        raise ValueError(f"not a caregiver event: {event}")
    from app.db.models.booking import BookingStatus
    if booking.status != BookingStatus.CANCELLED or not caregiver_id:
        return "stale"
    user_id = db.query(Caregiver.user_id).filter(Caregiver.id == caregiver_id).scalar()
    if user_id is None:
        return "stale"
    accepted = push_service.get_push_service().send_to_users(
        db, [user_id],
        title="Booking cancelled",
        body=f"Booking {booking.booking_code} has been cancelled.",  # the code only: no name, address or condition
        data={"type": "booking_cancelled", "booking_id": str(booking.id)},
    )
    return "sent" if accepted else "no_device"
