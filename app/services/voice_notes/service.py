"""The API side of voice notes: create, take the audio, retry, save, delete, list, and the care team's reads (plan sections 3 and 7).

Every function works on the caller's session and commits itself where the plan says a step is final. No function here logs, and none
puts text or audio in an audit row: ``detail`` holds ids, counts, sizes and codes.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import cast, func
from sqlalchemy.dialects.postgresql import INTEGER
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt, encrypt
from app.db.models.booking import Booking
from app.db.models.caregiver import Caregiver
from app.db.models.voice_note import VisitVoiceNote, VoiceNoteEvent
from app.services.voice_notes import access
from app.services.voice_notes.audio_store import AudioStore, park_audio
from app.services.voice_notes.constants import (
    AUDIO_TYPES, CONSENT_VERSIONS, FAILURES, STATUS_AWAITING, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY, STATUS_SAVED,
    SUMMARY_MAX_CHARS, TRANSCRIPT_MAX_CHARS,
)
from app.services.voice_notes.errors import not_found, refuse


# --- the audit trail --------------------------------------------------------------------------------------------------

def add_event(db: Session, *, voice_note_id: UUID, booking_id: UUID, actor_type: str, actor_id: Optional[UUID], action: str,
              detail: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> None:
    """One audit row, in the caller's transaction. ``detail`` must hold ids, counts, sizes and codes only: never text, never audio."""
    db.add(VoiceNoteEvent(
        voice_note_id=voice_note_id, booking_id=booking_id, actor_type=actor_type, actor_id=actor_id, action=action,
        detail=detail or {}, created_at=now or datetime.utcnow(),
    ))


# --- what the API returns ---------------------------------------------------------------------------------------------

def _text(value: Optional[str]) -> Optional[str]:
    return None if value is None else decrypt(value)


def note_view(note: VisitVoiceNote) -> Dict[str, Any]:
    """The ``VoiceNote`` object of plan section 3.1. Text only when ``ready`` or ``saved``; never audio, tokens or the model."""
    has_text = note.status in (STATUS_READY, STATUS_SAVED)
    error = None
    if note.status == STATUS_FAILED and note.error_code:
        message, retryable = FAILURES.get(note.error_code, FAILURES["transcription_failed"])
        error = {"code": note.error_code, "message": message, "retryable": retryable}
    return {
        "id": note.id,
        "booking_id": note.booking_id,
        "status": note.status,
        "language": note.language,
        "transcript": _text(note.transcript_encrypted) if has_text else None,
        "summary": _text(note.summary_encrypted) if has_text else None,
        "summary_available": has_text and note.summary_encrypted is not None,
        "edited": note.edited,
        "error": error,
        "duration_seconds": note.duration_seconds,
        "consent_version": note.consent_version,
        "consent_confirmed_at": note.consent_confirmed_at,
        "created_at": note.created_at,
        "ready_at": note.ready_at,
        "saved_at": note.saved_at,
        "expires_at": note.expires_at,
    }


def read_view(note: VisitVoiceNote) -> Dict[str, Any]:
    """What the rest of the care team sees of a saved note: no error, expiry or consent fields."""
    view = note_view(note)
    for hidden in ("error", "expires_at", "consent_version", "consent_confirmed_at"):
        del view[hidden]
    return view


# --- switches and caps ------------------------------------------------------------------------------------------------

def require_enabled() -> None:
    if not settings.VOICE_NOTES_ENABLED:
        raise refuse(503, "transcription_unavailable")


def _seconds_to_utc_midnight(now: datetime) -> int:
    midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    return max(1, int((midnight - now).total_seconds()))


def _check_daily_caps(db: Session, caregiver: Caregiver, duration_seconds: int, now: datetime) -> None:
    """Per-caregiver count and the all-users audio-seconds budget for the UTC day, read from the ``audio_received`` events (they
    survive a discarded note, so discarding never resets a cap). Counted when the audio arrives, not when the note is created."""
    day_start = datetime(now.year, now.month, now.day)
    mine = db.query(func.count(VoiceNoteEvent.id)).filter(
        VoiceNoteEvent.action == "audio_received", VoiceNoteEvent.actor_id == caregiver.user_id,
        VoiceNoteEvent.created_at >= day_start,
    ).scalar() or 0
    if mine >= settings.VOICE_NOTE_MAX_PER_CAREGIVER_PER_DAY:
        raise refuse(429, "daily_limit_reached", headers={"Retry-After": str(_seconds_to_utc_midnight(now))})
    used = db.query(func.coalesce(func.sum(cast(VoiceNoteEvent.detail["duration_seconds"].astext, INTEGER)), 0)).filter(
        VoiceNoteEvent.action == "audio_received", VoiceNoteEvent.created_at >= day_start,
    ).scalar() or 0
    if used + duration_seconds > settings.VOICE_NOTE_DAILY_AUDIO_SECONDS_CAP:
        raise refuse(503, "transcription_unavailable")


# --- create ------------------------------------------------------------------------------------------------------------

def create_note(db: Session, caregiver: Caregiver, booking_id: UUID, *, consent_confirmed: bool, consent_version: str,
                language: str, duration_seconds: int, client_request_id: Optional[UUID], now: datetime) -> Tuple[VisitVoiceNote, bool]:
    """Make a note in ``awaiting_audio`` and store the consent. Idempotent on ``(booking, client_request_id)``: a repeat returns the
    existing note (``created`` False). Returns ``(note, created)``."""
    require_enabled()
    booking = access.booking_for_create(db, caregiver, booking_id, now)
    if consent_confirmed is not True:
        raise refuse(400, "consent_required")
    if consent_version not in CONSENT_VERSIONS:
        raise refuse(400, "consent_version_unknown")
    if client_request_id is not None:
        existing = db.query(VisitVoiceNote).filter(
            VisitVoiceNote.booking_id == booking.id, VisitVoiceNote.client_request_id == client_request_id,
            VisitVoiceNote.caregiver_id == caregiver.id,
        ).first()
        if existing is not None:
            return existing, False
    count = db.query(func.count(VisitVoiceNote.id)).filter(VisitVoiceNote.booking_id == booking.id).scalar() or 0
    if count >= settings.VOICE_NOTE_MAX_PER_BOOKING:
        raise refuse(429, "note_limit_reached")
    note = VisitVoiceNote(
        booking_id=booking.id, caregiver_id=caregiver.id, client_request_id=client_request_id, status=STATUS_AWAITING,
        language=language, duration_seconds=duration_seconds, consent_confirmed_at=now, consent_version=consent_version,
        created_at=now, updated_at=now,
    )
    db.add(note)
    try:
        db.flush()
    except IntegrityError:
        # Two requests with the same client_request_id raced: the unique index let one in. The loser returns the winner's note.
        db.rollback()
        winner = db.query(VisitVoiceNote).filter(
            VisitVoiceNote.booking_id == booking.id, VisitVoiceNote.client_request_id == client_request_id,
            VisitVoiceNote.caregiver_id == caregiver.id,
        ).first()
        if winner is None:
            raise
        return winner, False
    add_event(db, voice_note_id=note.id, booking_id=booking.id, actor_type="caregiver", actor_id=caregiver.user_id, action="created",
              detail={"language": language, "duration_seconds": duration_seconds, "consent_version": consent_version}, now=now)
    db.commit()
    return note, True


# --- the audio ---------------------------------------------------------------------------------------------------------

def normalise_mime(content_type: Optional[str]) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def looks_like(mime: str, data: bytes) -> bool:
    """A cheap check that the first bytes are the container the header claims (plan section 5.1)."""
    if mime in ("audio/mp4", "audio/x-m4a"):
        return data[4:8] == b"ftyp"
    if mime == "audio/aac":
        return len(data) > 1 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0
    if mime == "audio/mpeg":
        return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)
    if mime == "audio/wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    return False


def prepare_audio(db: Session, caregiver: Caregiver, note_id: UUID, *, content_type: Optional[str], declared_length: Optional[int],
                  now: datetime) -> Tuple[VisitVoiceNote, str]:
    """Everything that can be refused before a byte of the body is read. Returns the note and the normalised type."""
    require_enabled()
    note = access.note_for_caregiver(db, caregiver, note_id, now)
    if note.status == STATUS_PROCESSING:
        raise refuse(409, "already_processing")
    if note.status not in (STATUS_AWAITING, STATUS_FAILED):
        raise refuse(409, "invalid_state")
    require_consent(note)
    mime = normalise_mime(content_type)
    if mime not in AUDIO_TYPES:
        raise refuse(415, "unsupported_audio_type")
    if declared_length is not None and declared_length > settings.VOICE_NOTE_MAX_BYTES:
        raise refuse(413, "audio_too_large")
    _check_daily_caps(db, caregiver, note.duration_seconds, now)
    return note, mime


def require_consent(note: Any) -> None:
    """Defence in depth: the column is NOT NULL, so this only fires if a row somehow lacks the flag."""
    if note.consent_confirmed_at is None:
        raise refuse(409, "consent_missing")


def accept_audio(db: Session, caregiver: Caregiver, note_id: UUID, audio: bytes, mime: str, store: AudioStore, now: datetime) -> VisitVoiceNote:
    """Park the encrypted audio and move the note to ``processing``, in one transaction under the note's row lock: a second upload that
    raced this one waits, then sees ``processing`` and is refused, so there is one job. If Redis fails nothing is committed."""
    note = access.note_for_caregiver(db, caregiver, note_id, now, lock=True)
    if note.status == STATUS_PROCESSING:
        raise refuse(409, "already_processing")
    if note.status not in (STATUS_AWAITING, STATUS_FAILED):
        raise refuse(409, "invalid_state")
    require_consent(note)
    try:
        park_audio(store, str(note.id), audio)
    except Exception:
        db.rollback()
        raise refuse(503, "transcription_unavailable") from None
    note.status = STATUS_PROCESSING
    note.audio_bytes = len(audio)
    note.audio_mime = mime
    note.error_code = None
    note.claimed_at = None
    note.attempts = 0
    note.expires_at = None
    note.updated_at = now
    add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="caregiver", actor_id=caregiver.user_id,
              action="audio_received", detail={"bytes": len(audio), "mime": mime, "duration_seconds": note.duration_seconds}, now=now)
    db.commit()
    return note


def mark_enqueue_failed(db: Session, note_id: UUID, now: datetime) -> None:
    """The queue refused the job: the note fails at once as retryable (the audio is still parked), instead of hanging in processing."""
    note = db.query(VisitVoiceNote).filter(VisitVoiceNote.id == note_id).with_for_update().first()
    if note is None or note.status != STATUS_PROCESSING:
        return
    note.status = STATUS_FAILED
    note.error_code = "provider_unavailable"
    note.expires_at = now + timedelta(hours=settings.VOICE_NOTE_DRAFT_TTL_HOURS)
    note.updated_at = now
    add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="system", actor_id=None,
              action="transcription_failed", detail={"code": "provider_unavailable", "attempts": 0, "enqueue": False}, now=now)
    db.commit()


def retry_note(db: Session, caregiver: Caregiver, note_id: UUID, store: AudioStore, now: datetime) -> VisitVoiceNote:
    """Run a failed note again from the parked audio. Only for a retryable failure whose audio is still stored."""
    require_enabled()
    note = access.note_for_caregiver(db, caregiver, note_id, now, lock=True)
    retryable = note.status == STATUS_FAILED and FAILURES.get(note.error_code or "", ("", False))[1]
    if not retryable:
        raise refuse(409, "invalid_state")
    if not store.exists(str(note.id)):
        raise refuse(409, "audio_expired")
    note.status = STATUS_PROCESSING
    note.error_code = None
    note.claimed_at = None
    note.attempts = 0
    note.expires_at = None
    note.updated_at = now
    add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="caregiver", actor_id=caregiver.user_id, action="retried",
              detail={}, now=now)
    db.commit()
    return note


# --- save, delete, list -----------------------------------------------------------------------------------------------

def save_note(db: Session, caregiver: Caregiver, note_id: UUID, *, transcript: str, summary: str, now: datetime) -> VisitVoiceNote:
    """Save the caregiver's version of a ``ready`` note. Saving the same text again is a no-op; different text is ``already_saved``."""
    note = access.note_for_caregiver(db, caregiver, note_id, now, lock=True)
    transcript, summary = transcript.strip(), summary.strip()
    if note.status == STATUS_SAVED:
        if transcript == _text(note.transcript_encrypted) and summary == (_text(note.summary_encrypted) or ""):
            return note
        raise refuse(409, "already_saved")
    if note.status != STATUS_READY:
        raise refuse(409, "not_ready")
    draft_transcript = _text(note.transcript_encrypted) or ""
    draft_summary = _text(note.summary_encrypted) or ""
    note.edited = transcript != draft_transcript.strip() or summary != draft_summary.strip()
    note.transcript_encrypted = encrypt(transcript)
    note.summary_encrypted = encrypt(summary)
    note.status = STATUS_SAVED
    note.saved_at = now
    note.expires_at = None
    note.retain_until = now + timedelta(days=settings.VOICE_NOTE_RETENTION_DAYS)
    note.updated_at = now
    add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="caregiver", actor_id=caregiver.user_id, action="saved",
              detail={"edited": note.edited}, now=now)
    db.commit()
    return note


def delete_note(db: Session, caregiver: Caregiver, note_id: UUID, store: AudioStore, now: datetime) -> None:
    """Discard (an unsaved note) or delete (a saved one, inside the caregiver's window): the text, the parked audio, the row."""
    note = access.note_for_caregiver(db, caregiver, note_id, now, lock=True)
    action = "deleted" if note.status == STATUS_SAVED else "discarded"
    add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="caregiver", actor_id=caregiver.user_id, action=action,
              detail={"status": note.status}, now=now)
    key = str(note.id)
    db.delete(note)
    db.commit()
    try:
        store.delete(key)
    except Exception:
        pass  # the key's TTL removes it; the note is already gone


def list_own(db: Session, caregiver: Caregiver, booking_id: UUID, now: datetime) -> List[VisitVoiceNote]:
    booking = access.booking_for_caregiver_reads(db, caregiver, booking_id, now)
    return (
        db.query(VisitVoiceNote)
        .filter(VisitVoiceNote.booking_id == booking.id, VisitVoiceNote.caregiver_id == caregiver.id)
        .order_by(VisitVoiceNote.created_at.desc(), VisitVoiceNote.id.desc()).all()
    )


def saved_notes_for_booking(db: Session, booking: Booking) -> List[VisitVoiceNote]:
    return (
        db.query(VisitVoiceNote)
        .filter(VisitVoiceNote.booking_id == booking.id, VisitVoiceNote.status == STATUS_SAVED)
        .order_by(VisitVoiceNote.saved_at.desc(), VisitVoiceNote.id.desc()).all()
    )


def record_reads(db: Session, notes: List[VisitVoiceNote], *, actor_type: str, actor_id: Optional[UUID], now: datetime) -> None:
    """One ``read`` event per note a non-author read, in the same transaction as the read."""
    for note in notes:
        add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type=actor_type, actor_id=actor_id, action="read",
                  detail={}, now=now)
