"""The ARQ queue the webhook hands inbound messages to.

The webhook returns 200 as soon as a message is recorded and enqueued; the conversation logic runs in
the worker (``app.worker.process_whatsapp_inbound_job``). ``get_inbound_queue`` is a FastAPI dependency
so tests replace it with an in-memory fake.
"""

from typing import Optional, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings

JOB_NAME = "process_whatsapp_inbound_job"


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
        # _job_id makes ARQ itself refuse a second job for the same Meta message id: a second layer
        # of dedupe behind the database's unique constraint.
        await pool.enqueue_job(JOB_NAME, job, _job_id=f"wa:{job['meta_message_id']}")


_queue = ArqInboundQueue()


def get_inbound_queue() -> InboundQueue:
    return _queue
