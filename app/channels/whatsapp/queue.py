"""The ARQ queue the webhook hands inbound messages to.

The webhook returns 200 as soon as a message is recorded and enqueued; the conversation logic runs in
the worker (``app.worker.process_whatsapp_inbound_job``). ``get_inbound_queue`` is a FastAPI dependency
so tests replace it with an in-memory fake.

Where the message text lives (WA-NFR-6: no message body at rest)
- ARQ keeps a job's arguments in Redis while it is queued and, by default, for an hour afterwards in the
  result record, and it logs them when the job starts. So the ARQ job carries ONLY the Meta message id.
- The text (or media caption), the interactive reply id and the message type go in a separate key,
  ``wa:inbound-body:<meta id>``, with a short TTL. The worker reads it, and deletes it when the job is
  finished for good (done, or out of tries). A retry keeps it, and refreshes the TTL.
- Results are not kept (``keep_result=0``), so nothing about the message remains once it is processed. The
  database row is the one record that the message was seen, and the one dedupe that matters.
"""

import json
import logging
from typing import Any, Dict, Optional, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings

logger = logging.getLogger(__name__)

JOB_NAME = "process_whatsapp_inbound_job"
BODY_KEY_PREFIX = "wa:inbound-body:"
BODY_TTL_SECONDS = 300  # the full retry schedule is ~150 s; a retry also refreshes the TTL
BODY_FIELDS = ("type", "text", "reply_id", "interactive_type")


class QueueRefused(RuntimeError):
    """ARQ did not accept the job (a job with this id is already there): treated as a failed enqueue."""


def body_key(meta_message_id: str) -> str:
    return BODY_KEY_PREFIX + meta_message_id


class InboundQueue(Protocol):
    async def enqueue(self, job: dict) -> None: ...


class ArqInboundQueue:
    def __init__(self) -> None:
        self._pool: Optional[ArqRedis] = None

    async def _get_pool(self) -> ArqRedis:
        if self._pool is None:
            from app.worker import json_deserializer, json_serializer  # same JSON codec as the worker

            self._pool = await create_pool(
                RedisSettings.from_dsn(settings.REDIS_URL),
                job_serializer=json_serializer, job_deserializer=json_deserializer,
            )
        return self._pool

    async def enqueue(self, job: dict) -> None:
        pool = await self._get_pool()
        meta_message_id = job["meta_message_id"]
        key = body_key(meta_message_id)
        body: Dict[str, Any] = {field: job.get(field) for field in BODY_FIELDS}
        await pool.set(key, json.dumps(body), ex=BODY_TTL_SECONDS)
        try:
            # _job_id makes ARQ itself refuse a second job for the same Meta message id. With results not
            # kept, an id is only "taken" while a job is queued or running, never after it finished.
            queued = await pool.enqueue_job(JOB_NAME, {"meta_message_id": meta_message_id},
                                            _job_id=f"wa:{meta_message_id}")
        except Exception:
            await self._discard(pool, key)
            raise
        if queued is None:
            # An earlier (possibly half-failed) attempt's job is still there. Do not claim success: the
            # endpoint answers 503 and forgets the row, and Meta's next retry is queued once that job is gone.
            await self._discard(pool, key)
            raise QueueRefused(f"a job for {meta_message_id} is already queued")

    @staticmethod
    async def _discard(pool: ArqRedis, key: str) -> None:
        try:
            await pool.delete(key)
        except Exception:
            logger.warning("whatsapp body key could not be removed; it expires on its own", exc_info=True)


_queue = ArqInboundQueue()


def get_inbound_queue() -> InboundQueue:
    return _queue
