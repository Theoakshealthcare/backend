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

from app.channels.whatsapp.notifications import run_events
from app.channels.whatsapp.processor import process_inbound
from app.channels.whatsapp.queue import body_key
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.booking_expiry import expire_unpaid_bookings
from app.services.dispatch import dispatch_tick
from app.services.dropout import tick as dropout_tick
from app.services.earnings import release_due as release_due_earnings
from app.services.kyc_events import KycRetry, process_event as process_kyc_event
from app.services.kyc_hooks import KycHooks
from app.services.kyc_reconcile import reconcile as kyc_reconcile
from app.services.payouts import create_scheduled_batch, dispatch_due as dispatch_payouts, reconcile as reconcile_payouts
from app.services.refunds import dispatch_due as dispatch_refunds, reconcile as reconcile_refunds

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


async def dropout_tick_job(ctx: dict) -> int:
    """The time-driven steps after a caregiver drops out: offer the family the choice when nobody has accepted close to
    the visit, and hand over to a person at the start time."""
    changed = await asyncio.to_thread(_run_with_session, dropout_tick)
    if changed:
        logger.info("dropout_tick: moved %d booking(s)", changed)
    return changed


async def earnings_release_job(ctx: dict) -> int:
    """Move caregiver earnings whose hold period has passed to the withdrawable balance (held and voided ones stay)."""
    released = await asyncio.to_thread(_run_with_session, release_due_earnings)
    if released:
        logger.info("earnings_release: released %d earning(s)", released)
    return released


async def payout_batch_job(ctx: dict) -> int:
    """On a payout day (per the booking policy) make the batch of caregiver payouts and reserve the money. An admin must
    approve the batch before anything is sent."""
    batch = await asyncio.to_thread(_run_with_session, create_scheduled_batch)
    if batch is not None:
        logger.info("payout_batch: made batch %s", batch.period_key)
    return 0 if batch is None else 1


async def payout_dispatch_job(ctx: dict) -> int:
    """Send approved payouts to Paystack (one transfer each, with the payout's own reference)."""
    sent = await asyncio.to_thread(_run_with_session, dispatch_payouts)
    if sent:
        logger.info("payout_dispatch: sent %d transfer(s)", sent)
    return sent


async def payout_reconcile_job(ctx: dict) -> int:
    """Ask Paystack about transfers that have had no webhook for a while and settle them."""
    settled = await asyncio.to_thread(_run_with_session, reconcile_payouts)
    if settled:
        logger.info("payout_reconcile: settled %d payout(s)", settled)
    return settled


async def refund_dispatch_job(ctx: dict) -> int:
    """Send approved refunds to Paystack (one refund each). A refund is claimed and committed before the call, so a retry of
    this job never sends one twice."""
    sent = await asyncio.to_thread(_run_with_session, dispatch_refunds)
    if sent:
        logger.info("refund_dispatch: sent %d refund(s)", sent)
    return sent


async def refund_reconcile_job(ctx: dict) -> int:
    """Ask Paystack about refunds that have had no webhook for a while and settle them (or flag them for review)."""
    settled = await asyncio.to_thread(_run_with_session, reconcile_refunds)
    if settled:
        logger.info("refund_reconcile: acted on %d refund(s)", settled)
    return settled


async def whatsapp_events_job(ctx: dict) -> dict:
    """Booking messages that are due (payment received, link expiring, booking cancelled unpaid) and the sweep of
    drafts nobody finished. Runs every 15 seconds; claims make overlapping runs safe."""
    counts = await asyncio.to_thread(_run_with_session, run_events)
    if any(counts.values()):
        logger.info("whatsapp_events: %s", counts)
    return counts


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


async def kyc_process_event(ctx: dict, job: dict) -> str:
    """One Prembly webhook event, queued by the endpoint (app/api/v1/endpoints/kyc_webhook.py): find the attempt, PULL the session
    from Prembly and record what it says (app/services/kyc_events.py). It decides nothing about the caregiver (that is K4). A pull
    that fails in a retryable way is retried with a delay; after the last try the ledger row is closed as ``error`` and the
    reconcile cron finds the attempt from Prembly's side. The job arguments are opaque ids only (no body, no personal data)."""
    tries = ctx.get("job_try", 1)
    final_try = tries >= WorkerSettings.max_tries
    try:
        result = await asyncio.to_thread(
            _run_with_session, lambda db: process_kyc_event(db, job, final_try=final_try, hooks=KycHooks.production()))
    except KycRetry as retry:
        logger.warning("kyc_process_event: pull failed, retrying (try %d of %d)", tries, WorkerSettings.max_tries)
        raise Retry(defer=retry.delay_seconds)
    logger.info("kyc_process_event: %s", result.outcome)
    return result.outcome


async def kyc_reconcile_job(ctx: dict) -> dict:
    """The backstop for lost webhooks (app/services/kyc_reconcile.py): finish attempts whose result was never processed, bind finished
    sessions Prembly never delivered, and expire attempts nobody answered. Idempotent and lock-safe; it never raises Retry."""
    stats = await asyncio.to_thread(_run_with_session, lambda db: kyc_reconcile(db, hooks=KycHooks.production()))
    return stats.as_dict()


def json_serializer(obj: Any) -> bytes:
    return json.dumps(obj, default=str).encode()


def json_deserializer(data: bytes) -> Any:
    # JSON instead of ARQ's default pickle: a job payload read from Redis can never execute code.
    return json.loads(data)


class WorkerSettings:
    functions = [expire_unpaid_bookings_job, dispatch_tick_job, dropout_tick_job, earnings_release_job,
                 payout_batch_job, payout_dispatch_job, payout_reconcile_job, refund_dispatch_job, refund_reconcile_job,
                 whatsapp_events_job,
                 func(process_whatsapp_inbound_job, keep_result=0),
                 func(kyc_process_event, keep_result=0),
                 kyc_reconcile_job]
    # Every minute, on the minute. A cron run is unique per scheduled minute across workers, and
    # the jobs are idempotent and lock-safe on top of that.
    cron_jobs = [
        cron(expire_unpaid_bookings_job, second=0),
        cron(dispatch_tick_job, second=0),
        # Offset from the others: it skips a booking another job has locked, and a job that started in the same second
        # (dispatch_tick locks every dispatching booking) would hide the same booking from it every minute.
        cron(dropout_tick_job, second=20),
        cron(earnings_release_job, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, second=40),  # every 5 minutes
        cron(payout_batch_job, hour=6, minute=0, second=10),  # 06:00:10 UTC every day; it acts only on a payout day
        cron(payout_dispatch_job, second=50),
        cron(payout_reconcile_job, minute={7, 22, 37, 52}, second=30),
        cron(refund_dispatch_job, second=40),
        cron(refund_reconcile_job, minute={7, 22, 37, 52}, second=45),
        cron(whatsapp_events_job, second={0, 15, 30, 45}),  # the payment reminder and confirmations are time-sensitive
        cron(kyc_reconcile_job, minute={3, 13, 23, 33, 43, 53}, second=15),  # every 10 minutes (design 6)
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    job_serializer = staticmethod(json_serializer)
    job_deserializer = staticmethod(json_deserializer)
    keep_result = 0  # no results at all: a failed job's result record would hold its arguments (WA-NFR-6)
    log_results = False
    job_timeout = 55  # finish inside the minute so runs never pile up
    max_jobs = 4
    max_tries = 5  # inbound WhatsApp messages retry on failure; the cron jobs are idempotent and never raise Retry
