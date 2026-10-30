"""The ARQ queue the Dojah webhook hands events to (the Meta webhook's ``InboundQueue`` pattern, app/channels/whatsapp/queue.py).

The ARQ job carries ONLY opaque facts from the signed body: the ledger row id and two flags (the webhook says "Failed", the webhook
says the flow ended). Not even the reference: the worker reads it from the ledger row. No name, e-mail, registry data or image link
ever goes through Redis (ARQ keeps arguments there and logs them), and results are not kept. If a job is lost (Redis wiped), the
ledger row stays unprocessed and the K4 reconcile cron runs it again, and pulls the attempt by its own reference anyway. ``get_kyc_event_queue`` is a FastAPI dependency so tests replace it with an in-memory fake.
"""
import logging
from typing import Optional, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings

logger = logging.getLogger(__name__)

JOB_NAME = "kyc_process_event"


class QueueRefused(RuntimeError):
    """ARQ did not accept the job (one with this id is already queued): treated as a failed enqueue."""


class KycEventQueue(Protocol):
    async def enqueue(self, job: dict) -> None: ...


class ArqKycEventQueue:
    def __init__(self) -> None:
        self._pool: Optional[ArqRedis] = None

    async def _get_pool(self) -> ArqRedis:
        if self._pool is None:
            from app.worker import json_deserializer, json_serializer  # the same JSON codec as the worker

            self._pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL),
                                           job_serializer=json_serializer, job_deserializer=json_deserializer)
        return self._pool

    async def enqueue(self, job: dict) -> None:
        pool = await self._get_pool()
        queued = await pool.enqueue_job(JOB_NAME, job, _job_id=f"kyc:{job['event_id']}")
        if queued is None:
            raise QueueRefused(f"a job for kyc event {job['event_id']} is already queued")


_queue = ArqKycEventQueue()


def get_kyc_event_queue() -> KycEventQueue:
    return _queue
