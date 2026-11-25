"""The time windows around a visit: when its chat can be written to and when a caregiver can still add or read notes.

One place for both, so the endpoints, the booking payloads and the voice-note access rules cannot drift apart.

* **Chat** (``chat_window``): writable while the booking is not terminal, and for ``CHAT_OPEN_HOURS_AFTER_VISIT`` after the visit was
  completed; after that it is read-only. A cancelled or declined booking is read-only at once. Reading history is never refused here
  (the routes keep their own participant check).
* **Notes** (``can_start_note``, ``can_read_notes``, ``notes_window``): the voice-note rules, now shared with typed notes. A note may
  be started while the visit is ``in_progress`` and for ``VOICE_NOTE_POST_VISIT_GRACE_MINUTES`` after it was completed; notes may be
  read, saved and deleted while the visit is live and for ``VOICE_NOTE_EDIT_WINDOW_HOURS`` after it ended. A caregiver who is no
  longer approved keeps only an ``in_progress`` visit (the care-plan rule).

All times are naive UTC, like every timestamp in this schema.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, status

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus
from app.db.models.caregiver import Caregiver

LIVE = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)
CHAT_CLOSED_MESSAGE = "This chat is closed. You can still read the conversation, but you can no longer send messages."
NOTES_CLOSED_MESSAGE = "Notes can be added during a visit and for a short time after it ends."


def ended_at(booking: Booking) -> datetime:
    """When the visit ended. Never ``updated_at``: any later edit of the booking would stretch a window. A completed booking with no
    recorded end falls back to when it was scheduled to end."""
    return booking.actual_end_time or booking.scheduled_end_date or booking.scheduled_date


@dataclass(frozen=True)
class Window:
    open: bool
    closes_at: Optional[datetime]  # null while the window has no fixed end (visit not over) or never opens


def chat_window(booking: Booking, now: datetime) -> Window:
    if booking.status in (BookingStatus.CANCELLED, BookingStatus.DECLINED):
        return Window(False, None)
    if booking.status == BookingStatus.COMPLETED:
        closes = ended_at(booking) + timedelta(hours=settings.CHAT_OPEN_HOURS_AFTER_VISIT)
        return Window(now <= closes, closes)
    return Window(True, None)


class ChatClosed(Exception):
    code = "chat_closed"
    message = CHAT_CLOSED_MESSAGE


def require_chat_open(booking: Booking, now: datetime) -> None:
    if not chat_window(booking, now).open:
        raise ChatClosed()


def chat_closed_error() -> HTTPException:
    """409 ``{"detail": {"code": "chat_closed", "message": ...}}``."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": ChatClosed.code, "message": ChatClosed.message})


def can_start_note(caregiver: Caregiver, booking: Booking, now: datetime) -> bool:
    if booking.status == BookingStatus.IN_PROGRESS:
        return True
    if caregiver.profile_status != "approved":
        return False  # a suspended caregiver has no access outside a visit in progress
    grace = timedelta(minutes=settings.VOICE_NOTE_POST_VISIT_GRACE_MINUTES)
    return booking.status == BookingStatus.COMPLETED and now <= ended_at(booking) + grace


def can_read_notes(caregiver: Caregiver, booking: Booking, now: datetime) -> bool:
    if booking.status == BookingStatus.IN_PROGRESS:
        return True
    if caregiver.profile_status != "approved":
        return False
    if booking.status in LIVE:
        return True
    window = timedelta(hours=settings.VOICE_NOTE_EDIT_WINDOW_HOURS)
    return booking.status == BookingStatus.COMPLETED and now <= ended_at(booking) + window


def notes_window(caregiver: Caregiver, booking: Booking, now: datetime) -> Window:
    """Can this caregiver add a note to the visit now, and until when (null while the visit is in progress: it closes
    ``VOICE_NOTE_POST_VISIT_GRACE_MINUTES`` after completion)."""
    closes = None
    if booking.status == BookingStatus.COMPLETED:
        closes = ended_at(booking) + timedelta(minutes=settings.VOICE_NOTE_POST_VISIT_GRACE_MINUTES)
    return Window(can_start_note(caregiver, booking, now), closes)


def notes_readable_until(booking: Booking) -> Optional[datetime]:
    """When the caregiver stops being able to read, save or delete their notes; null until the visit is completed."""
    if booking.status != BookingStatus.COMPLETED:
        return None
    return ended_at(booking) + timedelta(hours=settings.VOICE_NOTE_EDIT_WINDOW_HOURS)
