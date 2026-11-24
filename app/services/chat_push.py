"""Push the other person in a booking chat when a message is saved.

The chat is between exactly two people: the booking's patient and its assigned caregiver. A message from one pushes the
other (never the sender), through ``app.services.push`` (Expo; tokens, dead-token pruning and "no token, no push" are
handled there). Best effort by design: the message is already saved and the apps also poll the chat, so nothing in here
raises and a failure is only logged.

What the notification carries, and why:

- ``title``: the sender's first name. For a patient it is the first name plus the last initial ("Ada O."): the same
  privacy rule as before a visit is accepted, where a caregiver does not get a patient's full identity. A lock screen
  is a place strangers can read.
- ``body``: the message text, cut to ``BODY_CHARS`` characters on one line (an ellipsis marks a cut).
- ``data``: ``{"type": "chat_message", "booking_id": ...}`` so the app can open that conversation. No names, no text.

It runs after the response (``BackgroundTasks``) on its own database session, so a slow Expo never delays the sender.
"""

import logging
from typing import Optional
from uuid import UUID

from app.db.models.booking import Booking
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.db.models.user import User
from app.db.session import SessionLocal
from app.services import push as push_service

logger = logging.getLogger(__name__)

BODY_CHARS = 100
CHAT_MESSAGE_TYPE = "chat_message"
FALLBACK_TITLE = "New message"


def first_name(full_name: Optional[str]) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else ""


def patient_label(full_name: Optional[str]) -> str:
    """First name and last initial: "Ada Okafor" -> "Ada O.", "Ada" -> "Ada"."""
    parts = (full_name or "").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0].upper()}."


def body_preview(text: str) -> str:
    """The message on one line, at most BODY_CHARS characters; a cut message ends with an ellipsis (counted in the limit)."""
    flat = " ".join((text or "").split())
    if len(flat) <= BODY_CHARS:
        return flat
    return flat[: BODY_CHARS - 1].rstrip() + "…"


def push_new_message(booking_id: UUID, sender_user_id: UUID, sender_type: str, text: str) -> int:
    """Push the other party of the booking's chat about a message ``sender_user_id`` just sent. Returns how many pushes
    Expo accepted. Never raises."""
    db = SessionLocal()
    try:
        booking = db.query(Booking).filter(Booking.id == booking_id).first()
        if booking is None:
            return 0
        if sender_type == "patient":
            sender_name = patient_label(db.query(Patient.full_name).filter(Patient.id == booking.patient_id).scalar())
            recipient_id = (db.query(Caregiver.user_id).filter(Caregiver.id == booking.caregiver_id).scalar()
                            if booking.caregiver_id else None)
        elif sender_type == "caregiver":
            sender_name = first_name(db.query(Caregiver.full_name).filter(Caregiver.id == booking.caregiver_id).scalar())
            recipient_id = db.query(Patient.user_id).filter(Patient.id == booking.patient_id).scalar()
        else:
            return 0
        if recipient_id is None or recipient_id == sender_user_id:
            return 0
        if not db.query(User.id).filter(User.id == recipient_id, User.is_active.is_(True)).scalar():
            return 0  # an erased or deactivated account gets nothing
        return push_service.get_push_service().send_to_users(
            db, [recipient_id],
            title=sender_name or FALLBACK_TITLE,
            body=body_preview(text),
            data={"type": CHAT_MESSAGE_TYPE, "booking_id": str(booking_id)},
        )
    except Exception:
        logger.warning("push: chat push for booking %s failed; the message is saved", booking_id, exc_info=True)
        return 0
    finally:
        db.close()
