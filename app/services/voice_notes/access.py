"""Who may do what with a voice note (plan section 7). One refusal, one 404: a response never says whether a booking or a note exists.

* The **assigned caregiver** may start a note while the visit is ``in_progress`` (or inside ``VOICE_NOTE_POST_VISIT_GRACE_MINUTES`` after
  it was completed), and may read, save and delete their own notes while the visit is live and for ``VOICE_NOTE_EDIT_WINDOW_HOURS``
  after it ended. A suspended caregiver keeps only ``in_progress`` visits (the care-plan rule). Reassignment ends access at once.
* The **booker** (``patient_booking_cancel.is_booker``) reads saved notes of their booking, any time.
* An **admin** with ``clinical.read`` reads saved notes of any booking (the route checks the permission).
"""

from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.db.models.user import User
from app.db.models.voice_note import VisitVoiceNote
from app.services.patient_booking_cancel import is_booker
from app.services.voice_notes.errors import not_found, refuse

LIVE = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)


def caregiver_of(db: Session, user: User) -> Caregiver:
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == user.id).first()
    if caregiver is None:
        raise not_found()
    return caregiver


def _owned_booking(db: Session, caregiver: Caregiver, booking_id: UUID) -> Optional[Booking]:
    return (
        db.query(Booking).join(Patient, Patient.id == Booking.patient_id)
        .filter(Booking.id == booking_id, Booking.caregiver_id == caregiver.id, Patient.is_active.is_(True))
        .first()
    )


def _ended_at(booking: Booking) -> datetime:
    return booking.actual_end_time or booking.updated_at or datetime.utcnow()


def booking_for_create(db: Session, caregiver: Caregiver, booking_id: UUID, now: datetime) -> Booking:
    """The visit a new note is for. 404 when it is not the caller's; ``visit_not_active`` when it is theirs but not under way."""
    booking = _owned_booking(db, caregiver, booking_id)
    if booking is None:
        raise not_found()
    if booking.status == BookingStatus.IN_PROGRESS:
        return booking
    if caregiver.profile_status != "approved":
        raise not_found()  # a suspended caregiver has no access outside a visit in progress
    grace = timedelta(minutes=settings.VOICE_NOTE_POST_VISIT_GRACE_MINUTES)
    if booking.status == BookingStatus.COMPLETED and now <= _ended_at(booking) + grace:
        return booking
    raise refuse(409, "visit_not_active")


def _visible_to_caregiver(caregiver: Caregiver, booking: Booking, now: datetime) -> bool:
    if booking.status == BookingStatus.IN_PROGRESS:
        return True
    if caregiver.profile_status != "approved":
        return False
    if booking.status in LIVE:
        return True
    window = timedelta(hours=settings.VOICE_NOTE_EDIT_WINDOW_HOURS)
    return booking.status == BookingStatus.COMPLETED and now <= _ended_at(booking) + window


def booking_for_caregiver_reads(db: Session, caregiver: Caregiver, booking_id: UUID, now: datetime) -> Booking:
    booking = _owned_booking(db, caregiver, booking_id)
    if booking is None or not _visible_to_caregiver(caregiver, booking, now):
        raise not_found()
    return booking


def note_for_caregiver(db: Session, caregiver: Caregiver, note_id: UUID, now: datetime, lock: bool = False) -> VisitVoiceNote:
    """The caller's own note, or the one 404. ``lock`` takes the row lock (``FOR UPDATE``) for a write."""
    query = db.query(VisitVoiceNote).filter(VisitVoiceNote.id == note_id, VisitVoiceNote.caregiver_id == caregiver.id)
    if lock:
        query = query.with_for_update()
    note = query.populate_existing().first()
    if note is None:
        raise not_found()
    booking = _owned_booking(db, caregiver, note.booking_id)
    if booking is None or not _visible_to_caregiver(caregiver, booking, now):
        raise not_found()
    return note


def booking_for_booker(db: Session, user: User, booking_id: UUID) -> Booking:
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if booking is None or not is_booker(db, booking, user):
        raise not_found()
    return booking


def booking_for_admin(db: Session, booking_id: UUID) -> Booking:
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if booking is None:
        raise not_found()
    return booking
