"""The booker reads the saved voice notes of their booking (plan section 3.7). Drafts are never shown. Every read is an audit row."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user, get_db
from app.api.v1.endpoints.caregiver_care_plans import ScrubbedValidationRoute, _no_store
from app.db.models.user import User
from app.schemas.voice_note import VoiceNoteErrorBody, VoiceNoteReadList
from app.services.voice_notes import access, service

router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)


@router.get("/bookings/{booking_id}/voice-notes", response_model=VoiceNoteReadList,
            responses={404: {"model": VoiceNoteErrorBody, "description": "`not_found`: not the booker, or no such booking."}})
def booker_voice_notes(booking_id: UUID, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)) -> Any:
    booking = access.booking_for_booker(db, current_user, booking_id)
    notes = service.saved_notes_for_booking(db, booking)
    service.record_reads(db, notes, actor_type="patient", actor_id=current_user.id, now=datetime.utcnow())
    db.commit()
    return {"items": [service.read_view(note) for note in notes]}
