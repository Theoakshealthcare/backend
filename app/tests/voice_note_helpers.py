"""Shared setup for the voice note tests: an enabled feature, an in-memory audio store, an inline queue and ``FakeGemini``."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.config import settings
from app.db.models.booking import BookingStatus
from app.main import app as fastapi_app
from app.services.voice_notes import audio_store, provider
from app.services.voice_notes.processing import process_note
from app.services.voice_notes.queue import get_voice_note_queue
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient
from app.tests.fake_gemini import FAKE_AUDIO, FakeGemini

BASE = "/api/v1/caregiver"
CONSENT_VERSION = "2026-11-v1"


class InlineQueue:
    """Records the note ids handed to the worker; ``fail`` makes the next enqueue raise (the queue is down)."""

    def __init__(self):
        self.ids = []
        self.fail = False

    async def enqueue(self, note_id: str) -> None:
        if self.fail:
            raise ConnectionError("queue down")
        self.ids.append(note_id)


@pytest.fixture
def voice(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_NOTES_ENABLED", True)
    clock = SimpleNamespace(t=1_000_000.0)
    store = audio_store.MemoryAudioStore(clock=lambda: clock.t)
    queue = InlineQueue()
    gemini = FakeGemini()
    audio_store.set_audio_store_for_tests(store)
    provider.set_provider_for_tests(gemini)
    fastapi_app.dependency_overrides[get_voice_note_queue] = lambda: queue

    def run_job(note_id):
        return asyncio.run(process_note(UUID(str(note_id)), provider=gemini, store=store))

    yield SimpleNamespace(store=store, queue=queue, gemini=gemini, clock=clock, run_job=run_job)
    fastapi_app.dependency_overrides.pop(get_voice_note_queue, None)
    audio_store.set_audio_store_for_tests(None)
    provider.set_provider_for_tests(None)


def h(caregiver):
    return auth_headers(caregiver.user)


def visit(db, status=BookingStatus.IN_PROGRESS, **over):
    """An approved caregiver on a visit booked for oneself. Returns (caregiver, patient, booking)."""
    caregiver = make_caregiver(db)
    patient = make_patient(db, full_name="Placeholder Client")
    over.setdefault("booking_for", "myself")
    return caregiver, patient, make_booking(db, patient, caregiver, status=status, **over)


def create_body(**over):
    body = {"consent_confirmed": True, "consent_version": CONSENT_VERSION, "language": "en", "duration_seconds": 42,
            "client_request_id": "00000000-0000-4000-8000-00000000cccc"}
    body.update(over)
    return body


def create(client, caregiver, booking, **over):
    return client.post(f"{BASE}/visits/{booking.id}/voice-notes", json=create_body(**over), headers=h(caregiver))


def send_audio(client, caregiver, note_id, data=FAKE_AUDIO, content_type="audio/mp4"):
    return client.put(f"{BASE}/voice-notes/{note_id}/audio", content=data,
                      headers={**h(caregiver), "Content-Type": content_type})


def get_note(client, caregiver, note_id):
    return client.get(f"{BASE}/voice-notes/{note_id}", headers=h(caregiver))


def save(client, caregiver, note_id, transcript="Corrected text.", summary="Corrected summary."):
    return client.put(f"{BASE}/voice-notes/{note_id}", json={"transcript": transcript, "summary": summary}, headers=h(caregiver))


def ready_note(client, voice, caregiver, booking, **over):
    """Create, upload and transcribe a note (the fake provider's default text). Returns the note's id."""
    note_id = create(client, caregiver, booking, **over).json()["id"]
    assert send_audio(client, caregiver, note_id).status_code == 202
    assert voice.run_job(note_id).kind == "ready"
    return note_id


def code_of(response):
    return response.json()["detail"]["code"]


def later(hours=0, minutes=0):
    return datetime.utcnow() + timedelta(hours=hours, minutes=minutes)
