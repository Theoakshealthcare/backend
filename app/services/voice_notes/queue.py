"""The ARQ queue the audio upload hands a note to. The job carries ONLY the note id (like the WhatsApp inbound job): no audio, no text,
nothing derived from either. ``keep_result=0`` and ``log_results=False`` are already the worker's settings."""

import logging
from typing import Optional, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings

logger = logging.getLogger(__name__)

JOB_NAME = "transcribe_voice_note_job"


class VoiceNoteQueue(Protocol):
    async def enqueue(self, note_id: str) -> None: ...


class ArqVoiceNoteQueue:
    def __init__(self) -> None:
        self._pool: Optional[ArqRedis] = None

    async def _get_pool(self) -> ArqRedis:
        if self._pool is None:
            from app.worker import json_deserializer, json_serializer  # the worker's own JSON codec

            self._pool = await create_pool(
                RedisSettings.from_dsn(settings.REDIS_URL), job_serializer=json_serializer, job_deserializer=json_deserializer,
            )
        return self._pool

    async def enqueue(self, note_id: str) -> None:
        pool = await self._get_pool()
        # A job with this id already waiting means the note is already queued: enqueue_job answers None, which is fine.
        await pool.enqueue_job(JOB_NAME, note_id, _job_id=f"voice-note-{note_id}")


_queue: Optional[VoiceNoteQueue] = None


def get_voice_note_queue() -> VoiceNoteQueue:
    """A FastAPI dependency, so tests replace it with an in-memory fake."""
    global _queue
    if _queue is None:
        _queue = ArqVoiceNoteQueue()
    return _queue
