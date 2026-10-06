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


def test_a_queued_message_is_processed_by_a_real_worker_and_a_duplicate_is_not(db, fake_meta, redis_url, monkeypatch):
    from arq.connections import RedisSettings
    from arq.worker import Worker

    from app import worker
    from app.channels.whatsapp.ingest import ingest, parse_webhook
    from app.channels.whatsapp.queue import ArqInboundQueue

    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    wa_id = make_wa_id()
    jobs = ingest(db, parse_webhook(text_payload(wa_id, "Hi", message_id="wamid.REDIS1"))).jobs
    assert len(jobs) == 1

    async def run():
        queue = ArqInboundQueue()
        await queue.enqueue(jobs[0])
        await queue.enqueue(jobs[0])  # a replay: ARQ refuses the second job with the same id
        w = Worker(functions=worker.WorkerSettings.functions, redis_settings=RedisSettings.from_dsn(redis_url),
                   burst=True, poll_delay=0.1, max_tries=worker.WorkerSettings.max_tries,
                   job_serializer=worker.json_serializer, job_deserializer=worker.json_deserializer)
        try:
            await w.main()
            return w.jobs_complete
        finally:
            await w.close()
            await queue._pool.aclose()

    assert asyncio.run(run()) == 1
    db.expire_all()
    assert db.query(WhatsAppConversation).one().state == "consent_pending"
    assert len(fake_meta.bodies) == 1
    assert db.query(WhatsAppMessage).filter_by(meta_message_id="wamid.REDIS1").one().processed_at is not None
