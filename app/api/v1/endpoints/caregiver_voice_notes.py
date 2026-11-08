"""Caregiver voice notes: record during a visit, get an editable transcript and summary, save (plan section 3). Mounted under /caregiver.

Health data: every response is ``Cache-Control: no-store``, a 422 never echoes the rejected value, and no handler logs a body, text or
audio. The audio is read from the raw request body (never an ``UploadFile``, which Starlette spools to a temp file on disk above 1 MB),
encrypted, parked in Redis and deleted the moment the text exists.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.deps import get_current_caregiver, get_db
from app.api.v1.endpoints.caregiver_care_plans import ScrubbedValidationRoute, _no_store
from app.core.config import settings
from app.db.models.user import User
from app.schemas.voice_note import VoiceNoteCreate, VoiceNoteErrorBody, VoiceNoteList, VoiceNoteOut, VoiceNoteSave
from app.services.voice_notes import access, service
from app.services.voice_notes.audio_store import AudioStore, get_audio_store
from app.services.voice_notes.constants import AUDIO_TYPES
from app.services.voice_notes.errors import refuse
from app.services.voice_notes.queue import VoiceNoteQueue, get_voice_note_queue

router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)

ERRORS = {
    404: {"model": VoiceNoteErrorBody, "description": "`not_found`: one answer for no access of any kind."},
    409: {"model": VoiceNoteErrorBody, "description": "`visit_not_active`, `already_processing`, `invalid_state`, `not_ready`, `already_saved`, `audio_expired`, `consent_missing`."},
    429: {"model": VoiceNoteErrorBody, "description": "`note_limit_reached` or `daily_limit_reached` (with Retry-After)."},
    503: {"model": VoiceNoteErrorBody, "description": "`transcription_unavailable`: switched off, no capacity, or the audio store is down."},
}


def _now() -> datetime:
    return datetime.utcnow()


def _caregiver(db: Session, user: User):
    return access.caregiver_of(db, user)


def _out(note) -> Any:
    return service.note_view(note)


@router.post("/visits/{booking_id}/voice-notes", response_model=VoiceNoteOut, status_code=status.HTTP_201_CREATED,
             responses={200: {"description": "The note already existed for this `client_request_id`."}, **ERRORS})
def create_voice_note(booking_id: UUID, body: VoiceNoteCreate, response: Response, current_user: User = Depends(get_current_caregiver),
                      db: Session = Depends(get_db)) -> Any:
    """Make a note in `awaiting_audio` and store the consent. Idempotent on `client_request_id` (a repeat answers 200 with the same note)."""
    caregiver = _caregiver(db, current_user)
    note, created = service.create_note(
        db, caregiver, booking_id, consent_confirmed=body.consent_confirmed, consent_version=body.consent_version,
        language=body.language, duration_seconds=body.duration_seconds, client_request_id=body.client_request_id, now=_now(),
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return _out(note)


async def _read_capped(request: Request, max_bytes: int) -> bytes:
    """The raw body, or 413 ``audio_too_large`` as soon as it passes ``max_bytes`` (a chunked body is counted as it arrives)."""
    chunks: list = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise refuse(413, "audio_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


AUDIO_BODY = {
    "requestBody": {
        "required": True,
        "description": "The recording itself as the raw request body (not multipart). `Content-Type` is the audio type.",
        "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in AUDIO_TYPES},
    }
}


@router.put("/voice-notes/{note_id}/audio", response_model=VoiceNoteOut, status_code=status.HTTP_202_ACCEPTED,
            openapi_extra=AUDIO_BODY,
            responses={400: {"model": VoiceNoteErrorBody, "description": "`audio_empty`."},
                       413: {"model": VoiceNoteErrorBody, "description": "`audio_too_large`."},
                       415: {"model": VoiceNoteErrorBody, "description": "`unsupported_audio_type`."}, **ERRORS})
async def send_voice_note_audio(note_id: UUID, request: Request, current_user: User = Depends(get_current_caregiver),
                                db: Session = Depends(get_db), store: AudioStore = Depends(get_audio_store),
                                queue: VoiceNoteQueue = Depends(get_voice_note_queue)) -> Any:
    """Send the recording. Allowed from `awaiting_audio` and from `failed`. Resolves with the note in `processing`."""
    caregiver = _caregiver(db, current_user)
    declared = request.headers.get("content-length")
    note, mime = service.prepare_audio(
        db, caregiver, note_id, content_type=request.headers.get("content-type"),
        declared_length=int(declared) if declared and declared.isdigit() else None, now=_now(),
    )
    audio = await _read_capped(request, settings.VOICE_NOTE_MAX_BYTES)
    if not audio:
        raise refuse(400, "audio_empty")
    if not service.looks_like(mime, audio):
        raise refuse(415, "unsupported_audio_type")
    note = await run_in_threadpool(service.accept_audio, db, caregiver, note_id, audio, mime, store, _now())
    try:
        await queue.enqueue(str(note.id))
    except Exception:
        service.mark_enqueue_failed(db, note.id, _now())
        raise refuse(503, "transcription_unavailable") from None
    db.refresh(note)
    return _out(note)


@router.get("/voice-notes/{note_id}", response_model=VoiceNoteOut, responses=ERRORS)
def get_voice_note(note_id: UUID, current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db)) -> Any:
    """The poll: one of the caller's own notes."""
    return _out(access.note_for_caregiver(db, _caregiver(db, current_user), note_id, _now()))


@router.get("/visits/{booking_id}/voice-notes", response_model=VoiceNoteList, responses=ERRORS)
def list_voice_notes(booking_id: UUID, current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db)) -> Any:
    """The caller's own notes for a visit, newest first, so the app can resume after a restart."""
    notes = service.list_own(db, _caregiver(db, current_user), booking_id, _now())
    return {"items": [_out(note) for note in notes]}


@router.post("/voice-notes/{note_id}/retry", response_model=VoiceNoteOut, status_code=status.HTTP_202_ACCEPTED, responses=ERRORS)
async def retry_voice_note(note_id: UUID, current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db),
                           store: AudioStore = Depends(get_audio_store), queue: VoiceNoteQueue = Depends(get_voice_note_queue)) -> Any:
    """Run a failed note again. Only for a retryable failure whose audio is still stored; else `409 audio_expired` / `invalid_state`."""
    note = await run_in_threadpool(service.retry_note, db, _caregiver(db, current_user), note_id, store, _now())
    try:
        await queue.enqueue(str(note.id))
    except Exception:
        service.mark_enqueue_failed(db, note.id, _now())
        raise refuse(503, "transcription_unavailable") from None
    db.refresh(note)
    return _out(note)


@router.put("/voice-notes/{note_id}", response_model=VoiceNoteOut, responses={422: {"description": "Text limits."}, **ERRORS})
def save_voice_note(note_id: UUID, body: VoiceNoteSave, current_user: User = Depends(get_current_caregiver),
                    db: Session = Depends(get_db)) -> Any:
    """Save the caregiver's version of a `ready` note. The same text again is a 200; different text is `409 already_saved`."""
    note = service.save_note(db, _caregiver(db, current_user), note_id, transcript=body.transcript, summary=body.summary, now=_now())
    return _out(note)


@router.delete("/voice-notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
def delete_voice_note(note_id: UUID, current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db),
                      store: AudioStore = Depends(get_audio_store)) -> Response:
    """Discard an unsaved note, or delete a saved one inside the edit window: text, parked audio and row."""
    service.delete_note(db, _caregiver(db, current_user), note_id, store, _now())
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
