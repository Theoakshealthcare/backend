"""ARQ worker: the scheduled jobs. Run with ``arq app.worker.WorkerSettings``.

The job bodies are plain functions taking a Session (app/services/booking_expiry.py and
app/services/dispatch.py); this module only schedules them and owns the session lifecycle. They
are synchronous, so they run in a thread to keep the worker's event loop free.
"""

import asyncio
import json
import logging
from typing import Any, Callable

from arq import Retry, cron, func
from arq.connections import RedisSettings
from sqlalchemy.orm import Session

from app.channels.whatsapp.processor import process_inbound
from app.channels.whatsapp.queue import body_key
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.booking_expiry import expire_unpaid_bookings
from app.services.dispatch import dispatch_tick

logger = logging.getLogger(__name__)


def _run_with_session(job: Callable[[Session], Any]) -> Any:
    db = SessionLocal()
    try:
        return job(db)
    finally:
        db.close()


async def expire_unpaid_bookings_job(ctx: dict) -> int:
    cancelled = await asyncio.to_thread(_run_with_session, expire_unpaid_bookings)
    if cancelled:
        logger.info("expire_unpaid_bookings: cancelled %d booking(s)", cancelled)
    return cancelled


async def dispatch_tick_job(ctx: dict) -> int:
    advanced = await asyncio.to_thread(_run_with_session, dispatch_tick)
    if advanced:
        logger.info("dispatch_tick: advanced %d booking(s)", advanced)
    return advanced


WHATSAPP_RETRY_BASE_SECONDS = 5


async def process_whatsapp_inbound_job(ctx: dict, job: dict) -> None:
    """One inbound WhatsApp message, queued by the webhook. The ARQ job carries only the Meta message id; the
    text sits in a short-lived Redis key (see app/channels/whatsapp/queue.py) that is deleted once the job is
    finished for good. A failure is retried with growing delay (5s, 20s, 45s...) until ARQ's ``max_tries``;
    after the last try the message row stays unprocessed (that is the dead letter: the database, not Redis)."""
    redis = ctx["redis"]
    key = body_key(job["meta_message_id"])
    raw = await redis.get(key)
    # A missing key is not "no text": the processor decides (expired, or a race with an older job) and never
    # treats the message as having been read.
    full = {**job, **(json.loads(raw) if raw else {"body_missing": True})}
    tries = ctx.get("job_try", 1)
    try:
        await asyncio.to_thread(_run_with_session, lambda db: process_inbound(db, full))
    except Exception:
        logger.exception("process_whatsapp_inbound_job failed (try %d of %d)", tries, WorkerSettings.max_tries)
        if tries < WorkerSettings.max_tries:
            await redis.expire(key, settings.WHATSAPP_BODY_TTL_SECONDS)
            raise Retry(defer=WHATSAPP_RETRY_BASE_SECONDS * tries * tries)
        await redis.delete(key)
        raise
    await redis.delete(key)


def json_serializer(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode()


def json_deserializer(data: bytes) -> Any:
    # JSON instead of ARQ's default pickle: a job payload read from Redis can never execute code.
    return json.loads(data)


class WorkerSettings:
    functions = [expire_unpaid_bookings_job, dispatch_tick_job, func(process_whatsapp_inbound_job, keep_result=0)]
    # Every minute, on the minute. A cron run is unique per scheduled minute across workers, and
    # the jobs are idempotent and lock-safe on top of that.
    cron_jobs = [
        cron(expire_unpaid_bookings_job, second=0),
        cron(dispatch_tick_job, second=0),
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    job_serializer = staticmethod(json_serializer)
    job_deserializer = staticmethod(json_deserializer)
    keep_result = 0  # no results at all: a failed job's result record would hold its arguments (WA-NFR-6)
    log_results = False
    job_timeout = 55  # finish inside the minute so runs never pile up
    max_jobs = 4
    max_tries = 5  # inbound WhatsApp messages retry on failure; the cron jobs are idempotent and never raise Retry
