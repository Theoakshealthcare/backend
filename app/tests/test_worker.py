"""E3: the ARQ worker settings import cleanly, and one job runs end to end through a real Redis."""

import asyncio
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.tests.factories import make_booking, make_patient


def test_worker_settings_import_and_register_the_cron_jobs():
    from app import worker

    names = {c.coroutine.__name__ for c in worker.WorkerSettings.cron_jobs}
    assert names == {"expire_unpaid_bookings_job", "dispatch_tick_job", "whatsapp_events_job", "dropout_tick_job"}
    assert worker.WorkerSettings.redis_settings.host
    for cron_job in worker.WorkerSettings.cron_jobs:
        assert cron_job.minute is None
        if cron_job.coroutine.__name__ == "whatsapp_events_job":
            assert cron_job.second == {0, 15, 30, 45}  # booking messages are time-sensitive
        elif cron_job.coroutine.__name__ == "dropout_tick_job":
            assert cron_job.second not in (0, 15, 30, 45)  # never in the same second as a job that locks bookings
        else:
            assert cron_job.second == 0  # every minute


def test_redis_url_setting_has_a_local_default():
    assert settings.REDIS_URL.startswith("redis://")


@pytest.fixture(scope="module")
def redis_settings():
    # Module scope: the container starts before the per-test network guard is installed
    # (the Docker client itself talks HTTP over the Docker socket).
    from arq.connections import RedisSettings
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine", password="test-redis-password") as redis:
        yield RedisSettings(host=redis.get_container_host_ip(), port=int(redis.get_exposed_port(6379)),
                            password="test-redis-password")


def test_a_job_runs_through_a_real_redis(db, redis_settings):
    from arq import create_pool
    from arq.worker import Worker

    from app import worker

    expired = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING,
                           payment_expires_at=datetime.utcnow() - timedelta(minutes=1))

    async def run(redis_settings):
        pool = await create_pool(redis_settings, job_serializer=worker.json_serializer,
                                 job_deserializer=worker.json_deserializer)
        await pool.enqueue_job("expire_unpaid_bookings_job")
        w = Worker(functions=worker.WorkerSettings.functions, redis_settings=redis_settings,
                   burst=True, poll_delay=0.1,
                   job_serializer=worker.json_serializer, job_deserializer=worker.json_deserializer)
        try:
            await w.main()
        finally:
            await w.close()
            await pool.aclose()

    asyncio.run(run(redis_settings))

    db.expire_all()
    b = db.get(Booking, expired.id)
    assert b.status == BookingStatus.CANCELLED and b.cancel_reason == "unpaid_timeout"


def test_worker_uses_json_not_pickle():
    """A pickle payload planted in Redis must not be deserialised (pickle can execute code)."""
    import pickle

    import pytest as _pytest

    from app import worker

    assert worker.WorkerSettings.job_deserializer is worker.json_deserializer
    with _pytest.raises(ValueError):
        worker.WorkerSettings.job_deserializer(pickle.dumps({"f": "expire_unpaid_bookings_job"}))
    assert worker.json_deserializer(worker.json_serializer({"a": 1})) == {"a": 1}
