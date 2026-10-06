"""W1: webhook -> real Redis -> ARQ worker -> conversation, with the fake Meta Graph API at the end."""

import asyncio

import pytest

from app.core.config import settings
from app.db.models.whatsapp import WhatsAppConversation, WhatsAppMessage
from app.tests.factories_whatsapp import make_wa_id
from app.tests.fake_meta import fake_meta, text_payload  # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def redis_url():
    # Module scope: the container starts before the per-test network guard is installed.
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine", password="test-redis-password") as redis:
        yield f"redis://:test-redis-password@{redis.get_container_host_ip()}:{int(redis.get_exposed_port(6379))}/0"


def _worker(redis_url):
    from arq.worker import create_worker
    from arq.connections import RedisSettings

    from app import worker

    # Built from the production WorkerSettings, so the test exercises the real result/retention wiring.
    return create_worker(worker.WorkerSettings, redis_settings=RedisSettings.from_dsn(redis_url),
                         burst=True, poll_delay=0.1)


async def _run_worker(redis_url):
    w = _worker(redis_url)
    try:
        await w.main()
        return w
    finally:
        await w.close()


async def _everything_in_redis(redis_url) -> dict:
    """Every key with its raw DUMP (any type), so nothing can hide in a hash, list or sorted set."""
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url)
    try:
        return {k.decode(): await client.dump(k) async for k in client.scan_iter("*")}
    finally:
        await client.aclose()


def _ingest_one(db, wa_id, text, message_id):
    from app.channels.whatsapp.ingest import ingest, parse_webhook

    jobs = ingest(db, parse_webhook(text_payload(wa_id, text, name="Sentinel Name", message_id=message_id))).jobs
    assert len(jobs) == 1
    return jobs[0]


def test_a_queued_message_is_processed_by_a_real_worker_and_a_duplicate_is_not(db, fake_meta, redis_url, monkeypatch):
    from app.channels.whatsapp.queue import ArqInboundQueue

    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    job = _ingest_one(db, make_wa_id(), "Hi", "wamid.REDIS1")

    async def run():
        queue = ArqInboundQueue()
        await queue.enqueue(job)
        try:
            await queue.enqueue(job)  # a replay while the first is still queued: refused, never a second run
        except QueueRefused:
            pass
        try:
            return (await _run_worker(redis_url)).jobs_complete
        finally:
            await queue._pool.aclose()

    from app.channels.whatsapp.queue import QueueRefused

    assert asyncio.run(run()) == 1
    db.expire_all()
    assert db.query(WhatsAppConversation).one().state == "consent_pending"
    assert len(fake_meta.bodies) == 1
    assert db.query(WhatsAppMessage).filter_by(meta_message_id="wamid.REDIS1").one().processed_at is not None


SENTINEL = "SENTINELBODY7731"


def test_no_message_body_survives_in_redis_results_or_logs(db, fake_meta, redis_url, monkeypatch, caplog):
    """While queued the text lives in exactly one short-TTL key, never in the ARQ job; afterwards nowhere."""
    import logging

    from redis.asyncio import Redis

    from app.channels.whatsapp.queue import ArqInboundQueue

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    wa_id = make_wa_id()
    job = _ingest_one(db, wa_id, f"hello {SENTINEL}", "wamid.PRIV1")

    async def run():
        queue = ArqInboundQueue()
        await queue.enqueue(job)
        while_queued = await _everything_in_redis(redis_url)
        client = Redis.from_url(redis_url)
        ttl = await client.ttl("wa:inbound-body:wamid.PRIV1")
        await client.aclose()
        try:
            await _run_worker(redis_url)
        finally:
            await queue._pool.aclose()
        return while_queued, ttl, await _everything_in_redis(redis_url)

    while_queued, ttl, after = asyncio.run(run())

    holders = [k for k, v in while_queued.items() if SENTINEL.encode() in v]
    assert holders == ["wa:inbound-body:wamid.PRIV1"] and 0 < ttl <= 300
    assert not any(wa_id.encode() in v or b"Sentinel Name" in v for v in while_queued.values()
                   ), "the job on the queue must not carry the phone number or the profile name"

    assert not any(SENTINEL.encode() in v or wa_id.encode() in v for v in after.values())
    assert not [k for k in after if k.startswith(("wa:inbound-body:", "arq:result:", "arq:job:"))], list(after)
    assert SENTINEL not in caplog.text and wa_id not in caplog.text
    assert len(fake_meta.bodies) == 1  # and it was processed


def test_a_job_that_fails_every_try_leaves_no_body_and_no_result_behind(db, fake_meta, redis_url, monkeypatch, caplog):
    import logging

    from app import worker
    from app.channels.whatsapp.queue import ArqInboundQueue

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    monkeypatch.setattr(worker, "WHATSAPP_RETRY_BASE_SECONDS", 0)
    calls = []

    def boom(db_, job):
        calls.append(job)
        raise RuntimeError("db down")

    monkeypatch.setattr(worker, "process_inbound", boom)
    job = _ingest_one(db, make_wa_id(), f"hello {SENTINEL}", "wamid.PRIV2")

    async def run():
        queue = ArqInboundQueue()
        await queue.enqueue(job)
        try:
            await _run_worker(redis_url)
        finally:
            await queue._pool.aclose()
        return await _everything_in_redis(redis_url)

    after = asyncio.run(run())
    assert len(calls) == worker.WorkerSettings.max_tries
    assert all(c["text"] == f"hello {SENTINEL}" for c in calls)  # every retry still saw the body
    assert not any(SENTINEL.encode() in v for v in after.values()) and SENTINEL not in caplog.text
    assert not [k for k in after if k.startswith(("wa:inbound-body:", "arq:result:", "arq:job:"))], list(after)


def test_an_expired_body_is_handled_not_crashed_on(db, fake_meta, redis_url, monkeypatch):
    from app.channels.whatsapp.queue import ArqInboundQueue

    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    job = _ingest_one(db, make_wa_id(), "hello", "wamid.GONE1")

    async def run():
        queue = ArqInboundQueue()
        pool = await queue._get_pool()
        from app.channels.whatsapp.queue import JOB_NAME

        await pool.enqueue_job(JOB_NAME, {"meta_message_id": job["meta_message_id"]}, _job_id="wa:wamid.GONE1")
        try:
            return (await _run_worker(redis_url)).jobs_failed
        finally:
            await queue._pool.aclose()

    assert asyncio.run(run()) == 0
    db.expire_all()
    assert db.query(WhatsAppMessage).filter_by(meta_message_id="wamid.GONE1").one().processed_at is not None


def test_enqueue_that_writes_then_raises_is_retried_and_processed_exactly_once(db, fake_meta, redis_url, monkeypatch):
    """The 503 path with the real queue. ARQ may have stored the job even though the call raised; the retry
    must neither be swallowed (None from enqueue_job) nor run twice."""
    from app.channels.whatsapp.ingest import compensate
    from app.channels.whatsapp.queue import ArqInboundQueue, QueueRefused

    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    wa_id = make_wa_id()
    job = _ingest_one(db, wa_id, "Hi", "wamid.LOSS1")

    async def run():
        queue = ArqInboundQueue()
        pool = await queue._get_pool()
        real = pool.enqueue_job

        async def write_then_raise(*args, **kwargs):
            await real(*args, **kwargs)
            raise ConnectionError("reset after the write")

        monkeypatch.setattr(pool, "enqueue_job", write_then_raise)
        with pytest.raises(ConnectionError):
            await queue.enqueue(job)
        monkeypatch.setattr(pool, "enqueue_job", real)

        # The endpoint compensates (forgets the row); Meta retries while the orphan job is still queued.
        compensate(db, [job])
        retry = _ingest_one(db, wa_id, "Hi", "wamid.LOSS1")
        with pytest.raises(QueueRefused):  # None from enqueue_job is a failure, not "already handled"
            await queue.enqueue(retry)
        compensate(db, [retry])

        try:
            await _run_worker(redis_url)  # the orphan runs, finds no row, and is gone
            retry = _ingest_one(db, wa_id, "Hi", "wamid.LOSS1")  # Meta's next retry
            await queue.enqueue(retry)
            await _run_worker(redis_url)
        finally:
            await queue._pool.aclose()

    asyncio.run(run())
    db.expire_all()
    assert len(fake_meta.bodies) == 1  # processed once, replied once
    assert db.query(WhatsAppMessage).filter_by(meta_message_id="wamid.LOSS1", direction="inbound").one().processed_at
