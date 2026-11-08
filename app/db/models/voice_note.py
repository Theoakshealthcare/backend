"""AI voice notes (docs/plans/2026-11-07-ai-note-transcription.md section 4).

A caregiver records a note during a visit; the audio is sent once, turned into text by an external AI service, and deleted. The
transcript and the summary are Fernet-encrypted (``app.core.security``) and nullable so retention and erasure can wipe them. The
audio is never stored here: it waits, encrypted, in Redis until the text exists (``app.services.voice_notes.audio_store``).

``voice_note_events`` is the audit trail. It has no foreign key to the note, so it outlives the note it describes, and ``detail`` holds
ids, counts, sizes and codes: never text, never audio, never anything derived from them.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID

from app.db.base import Base

VOICE_NOTE_STATUSES = ("awaiting_audio", "processing", "ready", "failed", "saved")
VOICE_NOTE_LANGUAGES = ("en", "pcm")
VOICE_NOTE_ACTOR_TYPES = ("caregiver", "patient", "admin", "system")


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class VisitVoiceNote(Base):
    __tablename__ = "visit_voice_notes"
    __table_args__ = (
        CheckConstraint(_in("status", VOICE_NOTE_STATUSES), name="ck_visit_voice_notes_status"),
        CheckConstraint(_in("language", VOICE_NOTE_LANGUAGES), name="ck_visit_voice_notes_language"),
        CheckConstraint("duration_seconds BETWEEN 2 AND 600", name="ck_visit_voice_notes_duration"),
        Index("ix_visit_voice_notes_booking_created", "booking_id", "created_at"),
        Index("ix_visit_voice_notes_caregiver_created", "caregiver_id", "created_at"),
        Index("uq_visit_voice_notes_request", "booking_id", "client_request_id", unique=True,
              postgresql_where=text("client_request_id IS NOT NULL")),
        Index("ix_visit_voice_notes_open", "updated_at", postgresql_where=text("status IN ('awaiting_audio', 'processing')")),
        Index("ix_visit_voice_notes_retain", "retain_until", postgresql_where=text("status = 'saved'")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)  # the author
    client_request_id = Column(UUID(as_uuid=True), nullable=True)  # idempotency key from the app
    status = Column(String, nullable=False, default="awaiting_audio", server_default=text("'awaiting_audio'"))
    language = Column(String, nullable=False)
    duration_seconds = Column(Integer, nullable=False)  # the recorder's claim; the byte cap is the real limit
    # The consent flag, per recording: set when the note is created and never changed.
    consent_confirmed_at = Column(DateTime, nullable=False)
    consent_version = Column(String, nullable=False)
    audio_bytes = Column(Integer, nullable=True)  # the only facts kept about the audio
    audio_mime = Column(String, nullable=True)
    transcript_encrypted = Column(Text, nullable=True)  # the AI draft while ready, the caregiver's text once saved
    summary_encrypted = Column(Text, nullable=True)
    edited = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    error_code = Column(String, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    claimed_at = Column(DateTime, nullable=True)  # when a worker took the transcription (a second job sees it and stands down)
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)  # never returned: kept for cost and quality audits
    prompt_version = Column(String, nullable=True)
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    ready_at = Column(DateTime, nullable=True)
    saved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)  # when an unsaved note is deleted; null once saved
    retain_until = Column(DateTime, nullable=True)  # set at save


class VoiceNoteEvent(Base):
    __tablename__ = "voice_note_events"
    __table_args__ = (
        CheckConstraint(_in("actor_type", VOICE_NOTE_ACTOR_TYPES), name="ck_voice_note_events_actor"),
        Index("ix_voice_note_events_booking_created", "booking_id", "created_at"),
        # The daily caps count audio_received events (they survive a discarded note, so discarding never resets a cap).
        Index("ix_voice_note_events_action_created", "action", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    voice_note_id = Column(UUID(as_uuid=True), nullable=False)  # no foreign key: the row outlives the note
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    actor_type = Column(String, nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=True)  # user or admin id; null for the system
    action = Column(String, nullable=False)
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
