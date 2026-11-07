"""Request and response shapes of the voice note routes (docs/plans/2026-11-07-ai-note-transcription.md section 3)."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings
from app.services.voice_notes.constants import SUMMARY_MAX_CHARS, TRANSCRIPT_MAX_CHARS

VoiceNoteStatus = Literal["awaiting_audio", "processing", "ready", "failed", "saved"]


class VoiceNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consent_confirmed: bool  # must be true; false is 400 consent_required (a missing field is a 422)
    consent_version: str = Field(min_length=1, max_length=40)
    language: Literal["en", "pcm"]
    duration_seconds: int = Field(ge=settings.VOICE_NOTE_MIN_SECONDS, le=settings.VOICE_NOTE_MAX_SECONDS)
    client_request_id: Optional[UUID] = None


class VoiceNoteSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: str = Field(max_length=TRANSCRIPT_MAX_CHARS + 2_000)  # trimmed, then the real limit is checked below
    summary: str = Field(default="", max_length=SUMMARY_MAX_CHARS + 500)

    @field_validator("transcript")
    @classmethod
    def _transcript(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= TRANSCRIPT_MAX_CHARS:
            raise ValueError(f"must be 1 to {TRANSCRIPT_MAX_CHARS} characters")
        return value

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        value = value.strip()
        if len(value) > SUMMARY_MAX_CHARS:
            raise ValueError(f"must be at most {SUMMARY_MAX_CHARS} characters")
        return value


class VoiceNoteError(BaseModel):
    code: str
    message: str
    retryable: bool


class VoiceNoteOut(BaseModel):
    id: UUID
    booking_id: UUID
    status: VoiceNoteStatus
    language: Literal["en", "pcm"]
    transcript: Optional[str] = None
    summary: Optional[str] = None
    summary_available: bool
    edited: bool
    error: Optional[VoiceNoteError] = None
    duration_seconds: int
    consent_version: str
    consent_confirmed_at: datetime
    created_at: datetime
    ready_at: Optional[datetime] = None
    saved_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class VoiceNoteList(BaseModel):
    items: List[VoiceNoteOut]


class VoiceNoteReadOut(BaseModel):
    """A saved note as the rest of the care team sees it: no error, expiry or consent fields."""
    id: UUID
    booking_id: UUID
    status: VoiceNoteStatus
    language: Literal["en", "pcm"]
    transcript: Optional[str] = None
    summary: Optional[str] = None
    summary_available: bool
    edited: bool
    duration_seconds: int
    created_at: datetime
    ready_at: Optional[datetime] = None
    saved_at: Optional[datetime] = None


class VoiceNoteReadList(BaseModel):
    items: List[VoiceNoteReadOut]


class VoiceNoteErrorBody(BaseModel):
    """The shape of every error of these routes: ``{"detail": {"code", "message"}}``."""
    class Detail(BaseModel):
        code: str
        message: str

    detail: Detail
