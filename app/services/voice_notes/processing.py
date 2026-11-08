"""The worker side: transcribe one note, and sweep (plan sections 6.4 and 8).

``process_note`` is a plain async function taking the provider, the audio store and a session factory, so tests run it directly with
``FakeGemini`` and an in-memory store; the ARQ job in ``app/worker.py`` only supplies the production ones. It never holds a database
lock across the provider call: it claims the note (a short transaction), calls the provider, then locks the row again to write the
result and delete the audio.

Nothing here logs text, audio or a provider message. ``outcome.kind`` and codes are the only things reported.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional
from uuid import UUID

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import encrypt
from app.db.models.booking import Booking
from app.db.models.voice_note import VisitVoiceNote, VoiceNoteEvent
from app.db.session import SessionLocal
from app.services.voice_notes import prompts
from app.services.voice_notes.audio_store import AudioStore, read_audio
from app.services.voice_notes.constants import (
    FAILURES, PROVIDER_TEXT_MAX_CHARS, STATUS_AWAITING, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY, STATUS_SAVED,
)
from app.services.voice_notes.provider import (
    NoSpeech, ProviderBlocked, ProviderRefused, ProviderTimeout, ProviderTransient, TranscriptionProvider,
)
from app.services.voice_notes.service import add_event

logger = logging.getLogger(__name__)

CLAIM_STALE_AFTER = timedelta(seconds=180)  # longer than the job timeout: a second job stands down while one is working
SUMMARY_TIMEOUT_SECONDS = 30
TRANSIENT_MAX_ATTEMPTS = 3  # provider_unavailable after the third
TIMEOUT_MAX_ATTEMPTS = 2  # transcription_timeout after the second
RETRY_DEFER_SECONDS = (5, 30)
PROCESSING_STALE_AFTER = timedelta(minutes=5)
AWAITING_STALE_AFTER = timedelta(hours=1)


@dataclass(frozen=True)
class Outcome:
    """``skipped`` (not ours to do), ``gone`` (the note was discarded meanwhile), ``ready``, ``failed`` or ``retry`` (the job should
    run again after ``defer`` seconds)."""
    kind: str
    defer: int = 0
    code: Optional[str] = None


SessionFactory = Callable[[], Session]


def _claim(factory: SessionFactory, note_id: UUID, now: datetime) -> Optional[Dict[str, object]]:
    """Take the transcription: atomic, so two jobs for one note cannot both go on to call the provider."""
    db = factory()
    try:
        row = db.execute(
            update(VisitVoiceNote)
            .where(
                VisitVoiceNote.id == note_id, VisitVoiceNote.status == STATUS_PROCESSING,
                or_(VisitVoiceNote.claimed_at.is_(None), VisitVoiceNote.claimed_at < now - CLAIM_STALE_AFTER),
            )
            .values(claimed_at=now, attempts=VisitVoiceNote.attempts + 1, updated_at=now)
            .returning(VisitVoiceNote.attempts, VisitVoiceNote.audio_mime, VisitVoiceNote.language, VisitVoiceNote.booking_id)
        ).first()
        db.commit()
        if row is None:
            return None
        return {"attempts": row[0], "mime": row[1], "language": row[2], "booking_id": row[3]}
    finally:
        db.close()


def _release_claim(factory: SessionFactory, note_id: UUID, now: datetime) -> None:
    db = factory()
    try:
        db.execute(update(VisitVoiceNote).where(VisitVoiceNote.id == note_id, VisitVoiceNote.status == STATUS_PROCESSING)
                   .values(claimed_at=None, updated_at=now))
        db.commit()
    finally:
        db.close()


def _fail(factory: SessionFactory, store: AudioStore, note_id: UUID, code: str, attempts: int, now: datetime) -> Outcome:
    """Write a failed note. A retryable failure keeps the parked audio (so ``retry`` works; its TTL is refreshed once); any other
    failure deletes it."""
    retryable = FAILURES[code][1]
    db = factory()
    try:
        note = db.query(VisitVoiceNote).filter(VisitVoiceNote.id == note_id).with_for_update().first()
        if note is None or note.status != STATUS_PROCESSING:
            store.delete(str(note_id))
            return Outcome("gone" if note is None else "skipped")
        note.status = STATUS_FAILED
        note.error_code = code
        note.claimed_at = None
        note.expires_at = now + timedelta(hours=settings.VOICE_NOTE_DRAFT_TTL_HOURS)
        note.updated_at = now
        add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="system", actor_id=None,
                  action="transcription_failed", detail={"code": code, "attempts": attempts}, now=now)
        if retryable:
            # The parked audio's life is extended once, by the first failure; later failures never extend it again, so a note that
            # keeps failing cannot keep audio alive.
            db.flush()  # the session does not autoflush: count this failure's own event too
            if db.query(VoiceNoteEvent.id).filter(VoiceNoteEvent.voice_note_id == note_id, VoiceNoteEvent.action == "transcription_failed",
                                                  VoiceNoteEvent.detail["sweeper"].astext.is_(None)).count() == 1:
                store.touch(str(note_id), settings.VOICE_NOTE_AUDIO_TTL_SECONDS)
        else:
            store.delete(str(note_id))
            add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="system", actor_id=None,
                      action="audio_deleted", detail={"reason": "failed_final"}, now=now)
        db.commit()
        return Outcome("failed", code=code)
    finally:
        db.close()


async def _call(coro, timeout: float):
    return await asyncio.wait_for(coro, timeout=timeout)


async def process_note(note_id: UUID, *, provider: TranscriptionProvider, store: AudioStore,
                       session_factory: SessionFactory = SessionLocal, now: Callable[[], datetime] = datetime.utcnow) -> Outcome:
    claim = await asyncio.to_thread(_claim, session_factory, note_id, now())
    if claim is None:
        return Outcome("skipped")
    attempts = int(claim["attempts"])  # type: ignore[arg-type]
    if not settings.VOICE_NOTES_ENABLED:
        # The switch was turned off after this job was queued: no audio goes to the provider. The note fails as retryable.
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "provider_unavailable", attempts, now())

    audio = await asyncio.to_thread(read_audio, store, str(note_id))
    if audio is None:
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "transcription_failed", attempts, now())

    tokens_in = tokens_out = 0
    try:
        transcription = await _call(provider.transcribe(audio, str(claim["mime"]), str(claim["language"])),
                                    settings.VOICE_NOTE_PROVIDER_TIMEOUT_SECONDS)
    except NoSpeech:
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "no_speech_detected", attempts, now())
    except ProviderRefused:
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "audio_unsupported", attempts, now())
    except ProviderBlocked:
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "provider_blocked", attempts, now())
    except (ProviderTimeout, asyncio.TimeoutError):
        if attempts < TIMEOUT_MAX_ATTEMPTS:
            await asyncio.to_thread(_release_claim, session_factory, note_id, now())
            return Outcome("retry", defer=RETRY_DEFER_SECONDS[0])
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "transcription_timeout", attempts, now())
    except ProviderTransient:
        if attempts < TRANSIENT_MAX_ATTEMPTS:
            await asyncio.to_thread(_release_claim, session_factory, note_id, now())
            return Outcome("retry", defer=RETRY_DEFER_SECONDS[min(attempts, len(RETRY_DEFER_SECONDS)) - 1])
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "provider_unavailable", attempts, now())
    except Exception as exc:  # a provider bug: never log its message
        logger.warning("voice note provider raised %s", type(exc).__name__)
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "transcription_failed", attempts, now())

    text = (transcription.text or "").strip()
    if not text:
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "no_speech_detected", attempts, now())
    if len(text) > PROVIDER_TEXT_MAX_CHARS:
        return await asyncio.to_thread(_fail, session_factory, store, note_id, "transcription_failed", attempts, now())
    tokens_in += transcription.tokens_in or 0
    tokens_out += transcription.tokens_out or 0

    # The summary is a second, text-only call. Its failure never loses the transcript: the note is ready without a summary.
    summary_text: Optional[str] = None
    for _ in range(2):
        try:
            summary = await _call(provider.summarize(text, str(claim["language"])), SUMMARY_TIMEOUT_SECONDS)
            summary_text = (summary.text or "").strip() or None
            tokens_in += summary.tokens_in or 0
            tokens_out += summary.tokens_out or 0
            break
        except (ProviderTimeout, ProviderTransient, asyncio.TimeoutError):
            continue
        except Exception as exc:
            logger.warning("voice note summary raised %s", type(exc).__name__)
            break

    return await asyncio.to_thread(_finish, session_factory, store, note_id, provider, text, summary_text, attempts, tokens_in, tokens_out, now())


def _finish(factory: SessionFactory, store: AudioStore, note_id: UUID, provider: TranscriptionProvider, text: str,
            summary: Optional[str], attempts: int, tokens_in: int, tokens_out: int, now: datetime) -> Outcome:
    db = factory()
    try:
        note = db.query(VisitVoiceNote).filter(VisitVoiceNote.id == note_id).with_for_update().first()
        if note is None or note.status != STATUS_PROCESSING:
            store.delete(str(note_id))  # discarded while it was being transcribed: the text is dropped, never stored
            return Outcome("gone" if note is None else "skipped")
        note.transcript_encrypted = encrypt(text)
        note.summary_encrypted = encrypt(summary) if summary is not None else None
        note.status = STATUS_READY
        note.error_code = None
        note.claimed_at = None
        note.ready_at = now
        note.expires_at = now + timedelta(hours=settings.VOICE_NOTE_DRAFT_TTL_HOURS)
        note.provider = provider.name
        note.model = provider.model
        note.prompt_version = prompts.PROMPT_VERSION
        note.tokens_in = tokens_in or None
        note.tokens_out = tokens_out or None
        note.updated_at = now
        # The audio is deleted in the same step the text is stored (owner decision D1).
        store.delete(str(note_id))
        add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="system", actor_id=None, action="transcribed",
                  detail={"attempts": attempts, "summary_available": summary is not None, "tokens_in": tokens_in, "tokens_out": tokens_out}, now=now)
        add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="system", actor_id=None, action="audio_deleted",
                  detail={"reason": "transcribed"}, now=now)
        db.commit()
        return Outcome("ready")
    finally:
        db.close()


# --- the sweeper -------------------------------------------------------------------------------------------------------

def sweep(db: Session, store: AudioStore, now: Optional[datetime] = None) -> Dict[str, int]:
    """Plan section 8, every rule idempotent. Returns what each rule did (counts only)."""
    now = now or datetime.utcnow()
    counts = {"never_uploaded": 0, "stuck": 0, "draft_expired": 0, "retention": 0, "caregiver_changed": 0, "audio_orphans": 0}

    def purge(note: VisitVoiceNote, reason: str) -> None:
        add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="system", actor_id=None, action="purged",
                  detail={"reason": reason}, now=now)
        try:
            store.delete(str(note.id))
        except Exception:
            pass
        db.delete(note)

    for note in db.query(VisitVoiceNote).filter(VisitVoiceNote.status == STATUS_AWAITING,
                                                VisitVoiceNote.updated_at < now - AWAITING_STALE_AFTER).with_for_update(skip_locked=True).all():
        purge(note, "never_uploaded")
        counts["never_uploaded"] += 1

    for note in db.query(VisitVoiceNote).filter(
        VisitVoiceNote.status == STATUS_PROCESSING, VisitVoiceNote.updated_at < now - PROCESSING_STALE_AFTER,
        or_(VisitVoiceNote.claimed_at.is_(None), VisitVoiceNote.claimed_at < now - PROCESSING_STALE_AFTER),
    ).with_for_update(skip_locked=True).all():
        note.status = STATUS_FAILED
        note.error_code = "transcription_timeout"
        note.claimed_at = None
        note.expires_at = now + timedelta(hours=settings.VOICE_NOTE_DRAFT_TTL_HOURS)
        note.updated_at = now
        add_event(db, voice_note_id=note.id, booking_id=note.booking_id, actor_type="system", actor_id=None,
                  action="transcription_failed", detail={"code": "transcription_timeout", "attempts": note.attempts, "sweeper": True}, now=now)
        counts["stuck"] += 1

    for note in db.query(VisitVoiceNote).filter(VisitVoiceNote.status.in_((STATUS_READY, STATUS_FAILED)),
                                                VisitVoiceNote.expires_at.isnot(None),
                                                VisitVoiceNote.expires_at < now).with_for_update(skip_locked=True).all():
        purge(note, "draft_expired")
        counts["draft_expired"] += 1

    for note in db.query(VisitVoiceNote).filter(VisitVoiceNote.status == STATUS_SAVED, VisitVoiceNote.retain_until.isnot(None),
                                                VisitVoiceNote.retain_until < now).with_for_update(skip_locked=True).all():
        purge(note, "retention")
        counts["retention"] += 1

    for note in (
        db.query(VisitVoiceNote).join(Booking, Booking.id == VisitVoiceNote.booking_id)
        .filter(VisitVoiceNote.status != STATUS_SAVED, Booking.caregiver_id.isnot(None), Booking.caregiver_id != VisitVoiceNote.caregiver_id)
        .with_for_update(of=VisitVoiceNote, skip_locked=True).all()
    ):
        purge(note, "caregiver_changed")
        counts["caregiver_changed"] += 1

    db.flush()
    live = {str(i) for (i,) in db.query(VisitVoiceNote.id).filter(VisitVoiceNote.status.in_((STATUS_AWAITING, STATUS_PROCESSING, STATUS_FAILED)))}
    for key in store.list_ids():
        if key not in live:
            store.delete(key)
            add_event_orphan(db, key, now)
            counts["audio_orphans"] += 1
    db.commit()
    return counts


def add_event_orphan(db: Session, key: str, now: datetime) -> None:
    """An ``audio_deleted`` row for a parked key with no live note. It needs a booking id, which a vanished note cannot give, so the
    row is written only when an earlier event still names the note's booking."""
    try:
        note_id = UUID(key)
    except ValueError:
        return
    earlier = db.query(VoiceNoteEvent.booking_id).filter(VoiceNoteEvent.voice_note_id == note_id).order_by(VoiceNoteEvent.created_at).first()
    if earlier is not None:
        add_event(db, voice_note_id=note_id, booking_id=earlier[0], actor_type="system", actor_id=None, action="audio_deleted",
                  detail={"reason": "ttl"}, now=now)
