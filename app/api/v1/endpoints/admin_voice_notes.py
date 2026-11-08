"""An admin with ``clinical.read`` reads the saved voice notes of a booking (plan section 3.7). Each read is a ``voice_note_events`` row per
note and an ``admin_audit_log`` row (``voice_note.read`` on the booking, ids only)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.api.v1.endpoints.caregiver_care_plans import ScrubbedValidationRoute, _no_store
from app.db.models.admin import AdminUser
from app.schemas.voice_note import VoiceNoteErrorBody, VoiceNoteReadList
from app.services.audit import AuditService
from app.services.voice_notes import access, service

router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)


@router.get("/bookings/{booking_id}/voice-notes", response_model=VoiceNoteReadList,
            responses={404: {"model": VoiceNoteErrorBody, "description": "`not_found`: no such booking."}})
def admin_voice_notes(booking_id: UUID, request: Request, admin: AdminUser = Depends(require_permission("clinical.read")),
                      db: Session = Depends(get_db)) -> Any:
    booking = access.booking_for_admin(db, booking_id)
    notes = service.saved_notes_for_booking(db, booking)
    now = datetime.utcnow()
    service.record_reads(db, notes, actor_type="admin", actor_id=admin.id, now=now)
    AuditService(db).record(admin_user_id=admin.id, action="voice_note.read", entity_type="booking", entity_id=booking.id,
                            after={"booking_id": str(booking.id), "count": len(notes)}, request=request)
    db.commit()
    return {"items": [service.read_view(note) for note in notes]}
