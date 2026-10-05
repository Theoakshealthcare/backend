"""ARQ worker: the scheduled jobs. Run with ``arq app.worker.WorkerSettings``.

The job bodies are plain functions taking a Session (app/services/booking_expiry.py and
app/services/dispatch.py); this module only schedules them and owns the session lifecycle. They
are synchronous, so they run in a thread to keep the worker's event loop free.
"""

import asyncio
import json
import logging
from typing import Any, Callable

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.orm import Session

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


def json_serializer(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode()


def json_deserializer(data: bytes) -> Any:
    # JSON instead of ARQ's default pickle: a job payload read from Redis can never execute code.
    return json.loads(data)


class WorkerSettings:
    functions = [expire_unpaid_bookings_job, dispatch_tick_job]
    # Every minute, on the minute. A cron run is unique per scheduled minute across workers, and
    # the jobs are idempotent and lock-safe on top of that.
    cron_jobs = [
        cron(expire_unpaid_bookings_job, second=0),
        cron(dispatch_tick_job, second=0),
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    job_serializer = staticmethod(json_serializer)
    job_deserializer = staticmethod(json_deserializer)
    job_timeout = 55  # finish inside the minute so runs never pile up
    max_jobs = 4
