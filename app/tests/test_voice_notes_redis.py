"""Voice notes against a real Redis: the audio store, the ARQ job payload and the worker end to end (plan test 32)."""
import asyncio
import json

import pytest

from app.core.config import settings
from app.db.models.voice_note import VisitVoiceNote
from app.services.voice_notes import audio_store, provider
from app.services.voice_notes.audio_store import RedisAudioStore, park_audio, read_audio
from app.services.voice_notes.queue import ArqVoiceNoteQueue
from app.tests.fake_gemini import FAKE_AUDIO, SENTINEL_TRANSCRIPT, FakeGemini
from app.tests.voice_note_helpers import create, get_note, send_audio, visit


@pytest.fixture(scope="module")
def redis_url():
    # Module scope: the container starts before the per-test network guard is installed.
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine", password="test-redis-password") as redis:
        yield f"redis://:test-redis-password@{redis.get_container_host_ip()}:{int(redis.get_exposed_port(6379))}/0"


def test_the_redis_store_keeps_only_ciphertext_with_a_ttl_and_cleans_up(redis_url):
    import redis

    store = RedisAudioStore(redis_url)
    park_audio(store, "n1", FAKE_AUDIO)
    raw = redis.Redis.from_url(redis_url).get("vn:audio:n1")
    assert raw.startswith(b"gAAAAA") and b"SENTINEL-AUDIO" not in raw
    assert 0 < redis.Redis.from_url(redis_url).ttl("vn:audio:n1") <= settings.VOICE_NOTE_AUDIO_TTL_SECONDS
    assert read_audio(store, "n1") == FAKE_AUDIO
    assert store.list_ids() == ["n1"] and store.exists("n1")
    store.touch("n1", 5)
    assert redis.Redis.from_url(redis_url).ttl("vn:audio:n1") <= 5
    store.delete("n1")
    assert not store.exists("n1") and store.get("n1") is None and read_audio(store, "n1") is None


def test_the_slot_limiter_caps_concurrency_and_recovers(redis_url):
    store = RedisAudioStore(redis_url)
    import redis

    redis.Redis.from_url(redis_url).delete("vn:slots")
    a, b = store.acquire_slot(2), store.acquire_slot(2)
    assert a and b and store.acquire_slot(2) is None
    store.release_slot(a)
    assert store.acquire_slot(2)
    store.release_slot(b)
    store.release_slot(b)  # an extra release never goes below zero
    store.release_slot(b)
    assert store.acquire_slot(1) and store.acquire_slot(1) is None


def test_32_the_arq_job_and_redis_hold_only_the_note_id_and_the_worker_finishes_it(db, client, monkeypatch, redis_url):
    from arq.connections import RedisSettings, create_pool
    from arq.worker import create_worker

    from app import worker

    monkeypatch.setattr(settings, "VOICE_NOTES_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    store = RedisAudioStore(redis_url)
    gemini = FakeGemini()
    audio_store.set_audio_store_for_tests(store)
    provider.set_provider_for_tests(gemini)
    from app.main import app as fastapi_app
    from app.services.voice_notes.queue import get_voice_note_queue

    queue = ArqVoiceNoteQueue()
    fastapi_app.dependency_overrides[get_voice_note_queue] = lambda: queue
    try:
        cg, _, booking = visit(db)
        note_id = create(client, cg, booking).json()["id"]
        assert send_audio(client, cg, note_id).status_code == 202  # the real queue, the real Redis

        async def inspect_and_run():
            pool = await create_pool(RedisSettings.from_dsn(redis_url), job_serializer=worker.json_serializer,
                                     job_deserializer=worker.json_deserializer)
            jobs = await pool.queued_jobs()
            assert [(j.function, j.args, j.kwargs) for j in jobs] == [("transcribe_voice_note_job", [note_id], {})]
            import redis.asyncio as aredis

            client_ = aredis.Redis.from_url(redis_url)
            blob = b""
            async for key in client_.scan_iter(match="arq:*"):
                kind = await client_.type(key)
                if kind == b"string":
                    blob += await client_.get(key)
            assert b"SENTINEL" not in blob and b"gAAAAA" not in blob  # no audio, no ciphertext, no text in any job key
            assert note_id.encode() in blob
            await client_.aclose()
            w = create_worker(worker.WorkerSettings, redis_settings=RedisSettings.from_dsn(redis_url), burst=True, poll_delay=0.1,
                              cron_jobs=[])
            try:
                await w.main()
                return w
            finally:
                await w.close()

        asyncio.run(inspect_and_run())
        db.expire_all()
        assert db.get(VisitVoiceNote, note_id).status == "ready"
        body = get_note(client, cg, note_id).json()
        assert body["transcript"] == SENTINEL_TRANSCRIPT
        assert store.list_ids() == []  # the audio is gone from Redis
        import redis

        leftovers = [k for k in redis.Redis.from_url(redis_url).scan_iter(match="arq:result:*")]
        assert leftovers == []  # keep_result=0: no result record holds anything
    finally:
        fastapi_app.dependency_overrides.pop(get_voice_note_queue, None)
        audio_store.set_audio_store_for_tests(None)
        provider.set_provider_for_tests(None)
