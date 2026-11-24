"""The caregiver's conversation list: one row per booking chat they may open, newest activity first.

A caregiver may open a booking's chat only when the booking is theirs (``bookings.caregiver_id``, the exact check in
``send_chat_message`` and ``get_chat_messages`` in ``app/api/v1/endpoints/booking.py``): no status condition. That is also the
rule under which a caregiver sees the patient's identity (``redact_offer`` hides it from everyone who merely holds an
offer), so a conversation always shows the patient's full name; a booking that was only offered is not listed at all.

Two queries' worth of work in one statement: the newest message of every conversation comes from a single ``DISTINCT ON``
subquery joined to the bookings, so a page costs one round trip however many conversations it holds. Only the newest message
is read, and only its first characters; addresses, phone numbers and the rest of the thread never leave the database here.

Read state: ``chat_messages.is_read`` exists but nothing ever sets it (no route or service writes it), so an unread count
would be wrong. It is left out on purpose until read tracking exists.
"""

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.db.models.booking import Booking, ChatMessage
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.services.caregiver_clients import UNKNOWN_CLIENT_NAME

PREVIEW_CHARS = 140
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _preview(text: str) -> str:
    """At most PREVIEW_CHARS characters; a cut message ends with an ellipsis (which counts in the 140)."""
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[: PREVIEW_CHARS - 1].rstrip() + "…"


def list_chats(db: Session, caregiver: Caregiver, user_id: UUID, before: Optional[datetime], before_id: Optional[UUID],
               limit: int) -> Dict[str, Any]:
    """Newest activity first (the last message's time, else the booking's creation time), keyset paged on (activity, booking id):
    pass the previous page's ``next_before`` and ``next_before_id`` back as ``before`` and ``before_id``."""
    last = (
        db.query(
            ChatMessage.booking_id.label("booking_id"),
            func.left(ChatMessage.message, PREVIEW_CHARS + 1).label("body"),  # one over the limit tells "cut" from "exact"
            ChatMessage.created_at.label("sent_at"),
            ChatMessage.sender_id.label("sender_id"),
        )
        .join(Booking, Booking.id == ChatMessage.booking_id)
        .filter(Booking.caregiver_id == caregiver.id)
        .distinct(ChatMessage.booking_id)
        .order_by(ChatMessage.booking_id, ChatMessage.created_at.desc().nullslast(), ChatMessage.id.desc())
        .subquery("last_message")
    )
    activity = func.coalesce(last.c.sent_at, Booking.created_at)
    query = (
        db.query(
            Booking.id, Booking.service_type, Booking.status, Booking.scheduled_date, Patient.full_name,
            last.c.body, last.c.sent_at, last.c.sender_id, activity.label("activity"),
        )
        .outerjoin(Patient, Patient.id == Booking.patient_id)
        .outerjoin(last, last.c.booking_id == Booking.id)
        .filter(Booking.caregiver_id == caregiver.id)
    )
    if before is not None and before_id is not None:
        query = query.filter(tuple_(activity, Booking.id) < tuple_(before, before_id))
    elif before is not None:
        query = query.filter(activity < before)
    rows = query.order_by(activity.desc(), Booking.id.desc()).limit(limit + 1).all()
    more, rows = len(rows) > limit, rows[:limit]
    items = [
        {
            "booking_id": r.id,
            "patient_name": r.full_name or UNKNOWN_CLIENT_NAME,
            "service_type": r.service_type,
            "status": r.status,
            "scheduled_date": r.scheduled_date,
            "last_message": (
                {"text": _preview(r.body), "sent_at": r.sent_at, "from_me": r.sender_id == user_id}
                if r.body is not None and r.sent_at is not None else None
            ),
        }
        for r in rows
    ]
    return {"items": items, "next_before": rows[-1].activity if more else None,
            "next_before_id": rows[-1].id if more else None}
