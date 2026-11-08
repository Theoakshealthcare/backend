"""Writes the live captures behind docs/fe-integration-guide-caregiver-ai-notes.md.

Skipped in normal runs. Run it against the real app (the FastAPI app in-process, every route and dependency as in production) and a real
Postgres (the test harness):

    CAPTURE_VOICE_NOTES=1 poetry run pytest app/tests/test_capture_voice_notes.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver-ai-notes.md docs/fe-captures/caregiver-ai-notes`.

What is real and what is not (the guide repeats this): the HTTP layer, validation, access rules, the database, encryption, the state
machine, the worker's processing code and the audit rows are the real thing. The three fakes: the AI provider (``FakeGemini``, scripted
text), the audio store (in memory instead of Redis) and the queue (records the job; the test then runs the worker's processing function).
The real ARQ + Redis path is covered by app/tests/test_voice_notes_redis.py, and the real Gemini call has never run. All data is placeholder.
"""
import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import settings
from app.db.models.booking import BookingStatus
from app.main import app as fastapi_app
from app.services.voice_notes import audio_store, provider
from app.services.voice_notes.processing import process_note
from app.services.voice_notes.queue import get_voice_note_queue
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.fake_gemini import FAKE_AUDIO, FakeGemini
from app.tests.voice_note_helpers import BASE, InlineQueue

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_VOICE_NOTES"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "caregiver-ai-notes"
TRANSCRIPT = "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was one twenty over eighty. She said she slept well."
SUMMARY = "Placeholder client ate breakfast, took the morning tablets, blood pressure 120/80, slept well."
SAVED_TRANSCRIPT = "Placeholder client had breakfast at nine and took the morning tablets. Blood pressure was 120/80. She said she slept well."
INDEX = []


def _save(name, r, note=None):
    sent = r.request.content
    try:
        body = json.loads(sent) if sent else None
    except (ValueError, UnicodeDecodeError):
        body = f"<{len(sent)} bytes of raw audio, Content-Type {r.request.headers.get('content-type')}>"
    try:
        resp_body = r.json()
    except ValueError:
        resp_body = None
    doc = {
        "request": {"method": r.request.method, "path": r.request.url.path + (f"?{r.request.url.query.decode()}" if r.request.url.query else ""),
                    "auth": "Bearer <ACCESS_TOKEN_JWT>", "body": body},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control", "retry-after")},
                     "body": resp_body},
    }
    if note:
        doc["note"] = note
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    INDEX.append(name)
    return r


def _hdr(caregiver):
    return auth_headers(caregiver.user)


def _body(**over):
    body = {"consent_confirmed": True, "consent_version": "2026-11-v1", "language": "pcm", "duration_seconds": 42,
            "client_request_id": "11111111-2222-4333-8444-555555555555"}
    body.update(over)
    return body


def test_capture(db, client, monkeypatch):
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    monkeypatch.setattr(settings, "VOICE_NOTES_ENABLED", True)
    store = audio_store.MemoryAudioStore()
    queue = InlineQueue()
    gemini = FakeGemini(transcripts=[TRANSCRIPT] * 6, summaries=[SUMMARY] * 6)
    audio_store.set_audio_store_for_tests(store)
    provider.set_provider_for_tests(gemini)
    fastapi_app.dependency_overrides[get_voice_note_queue] = lambda: queue

    def run_job(note_id):
        return asyncio.run(process_note(UUID(str(note_id)), provider=gemini, store=store))

    try:
        caregiver = make_caregiver(db, full_name="Placeholder Caregiver")
        patient = make_patient(db, full_name="Placeholder Client")
        visit = make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS, booking_for="myself")
        accepted = make_booking(db, patient, caregiver, status=BookingStatus.ACCEPTED, booking_for="myself")
        other = make_caregiver(db, full_name="Placeholder Other Caregiver")
        url = f"{BASE}/visits/{visit.id}/voice-notes"
        audio = lambda note_id, data=FAKE_AUDIO, ctype="audio/mp4": client.put(  # noqa: E731
            f"{BASE}/voice-notes/{note_id}/audio", content=data, headers={**_hdr(caregiver), "Content-Type": ctype})

        # --- create ---
        created = _save("01-create-201.json", client.post(url, json=_body(), headers=_hdr(caregiver)))
        note_id = created.json()["id"]
        _save("02-create-repeat-200.json", client.post(url, json=_body(), headers=_hdr(caregiver)),
              "The same client_request_id again: 200 and the same note, nothing new is made.")
        _save("03-consent-false-400.json", client.post(url, json=_body(consent_confirmed=False, client_request_id=None), headers=_hdr(caregiver)))
        _save("04-consent-version-400.json", client.post(url, json=_body(consent_version="2020-01-v0", client_request_id=None), headers=_hdr(caregiver)))
        bad = _body(client_request_id=None)
        del bad["consent_confirmed"]
        _save("05-create-missing-field-422.json", client.post(url, json=bad, headers=_hdr(caregiver)))
        _save("06-create-duration-422.json", client.post(url, json=_body(duration_seconds=1, client_request_id=None), headers=_hdr(caregiver)))
        _save("07-create-visit-not-active-409.json", client.post(f"{BASE}/visits/{accepted.id}/voice-notes", json=_body(client_request_id=None), headers=_hdr(caregiver)))
        _save("08-create-not-found-404.json", client.post(url, json=_body(client_request_id=None), headers=_hdr(other)),
              "Someone else's visit. The same answer is sent for a visit that does not exist.")
        _save("09-poll-awaiting-audio.json", client.get(f"{BASE}/voice-notes/{note_id}", headers=_hdr(caregiver)))
        _save("10-save-not-ready-409.json", client.put(f"{BASE}/voice-notes/{note_id}", json={"transcript": "x", "summary": ""}, headers=_hdr(caregiver)))

        # --- audio ---
        _save("11-audio-empty-400.json", audio(note_id, b""))
        _save("12-audio-type-415.json", audio(note_id, ctype="application/pdf"))
        _save("13-audio-not-audio-415.json", audio(note_id, b"%PDF-1.4 this is not audio at all" + bytes(40)))
        monkeypatch.setattr(settings, "VOICE_NOTE_MAX_BYTES", 500)
        _save("14-audio-too-large-413.json", audio(note_id, FAKE_AUDIO + bytes(600)),
              note="Captured with the byte cap lowered to 500 so a small body shows it; the real cap is 12 MiB.")
        monkeypatch.setattr(settings, "VOICE_NOTE_MAX_BYTES", 12 * 1024 * 1024)
        _save("15-audio-202.json", audio(note_id))
        _save("16-audio-again-409.json", audio(note_id))
        _save("17-poll-processing.json", client.get(f"{BASE}/voice-notes/{note_id}", headers=_hdr(caregiver)))
        assert run_job(note_id).kind == "ready"
        _save("18-poll-ready.json", client.get(f"{BASE}/voice-notes/{note_id}", headers=_hdr(caregiver)))
        _save("19-list-200.json", client.get(url, headers=_hdr(caregiver)))

        # --- save ---
        edited = {"transcript": SAVED_TRANSCRIPT, "summary": SUMMARY}
        _save("20-save-200.json", client.put(f"{BASE}/voice-notes/{note_id}", json=edited, headers=_hdr(caregiver)))
        _save("21-save-same-again-200.json", client.put(f"{BASE}/voice-notes/{note_id}", json=edited, headers=_hdr(caregiver)))
        _save("22-save-different-409.json", client.put(f"{BASE}/voice-notes/{note_id}", json={"transcript": "Something else.", "summary": ""}, headers=_hdr(caregiver)))
        _save("23-save-empty-422.json", client.put(f"{BASE}/voice-notes/{note_id}", json={"transcript": "   ", "summary": ""}, headers=_hdr(caregiver)))

        # --- failures ---
        silent = client.post(url, json=_body(client_request_id="22222222-2222-4333-8444-555555555555"), headers=_hdr(caregiver)).json()["id"]
        audio(silent)
        gemini.fail_next("no_speech")
        run_job(silent)
        _save("24-poll-failed-no-speech.json", client.get(f"{BASE}/voice-notes/{silent}", headers=_hdr(caregiver)))
        _save("25-retry-not-retryable-409.json", client.post(f"{BASE}/voice-notes/{silent}/retry", headers=_hdr(caregiver)))
        busy = client.post(url, json=_body(client_request_id="33333333-2222-4333-8444-555555555555"), headers=_hdr(caregiver)).json()["id"]
        audio(busy)
        gemini.fail_next("transient", times=3)
        for _ in range(3):
            run_job(busy)
        _save("26-poll-failed-retryable.json", client.get(f"{BASE}/voice-notes/{busy}", headers=_hdr(caregiver)))
        _save("27-retry-202.json", client.post(f"{BASE}/voice-notes/{busy}/retry", headers=_hdr(caregiver)))
        run_job(busy)
        _save("28-poll-after-retry-ready.json", client.get(f"{BASE}/voice-notes/{busy}", headers=_hdr(caregiver)))
        gone = client.post(url, json=_body(client_request_id="44444444-2222-4333-8444-555555555555"), headers=_hdr(caregiver)).json()["id"]
        audio(gone)
        gemini.fail_next("transient", times=3)
        for _ in range(3):
            run_job(gone)
        store.delete(gone)  # the parked audio has expired
        _save("29-retry-audio-expired-409.json", client.post(f"{BASE}/voice-notes/{gone}/retry", headers=_hdr(caregiver)),
              note="The audio was removed from the store before this call, as its TTL would.")
        nosummary = client.post(url, json=_body(client_request_id="55555555-2222-4333-8444-555555555555"), headers=_hdr(caregiver)).json()["id"]
        audio(nosummary)
        gemini.fail_next("transient", times=2, target="summarize")
        run_job(nosummary)
        _save("30-poll-ready-without-summary.json", client.get(f"{BASE}/voice-notes/{nosummary}", headers=_hdr(caregiver)))

        # --- delete ---
        _save("31-delete-204.json", client.delete(f"{BASE}/voice-notes/{nosummary}", headers=_hdr(caregiver)))
        _save("32-delete-again-404.json", client.delete(f"{BASE}/voice-notes/{nosummary}", headers=_hdr(caregiver)))

        # --- the rest of the care team ---
        _save("33-patient-read-200.json", client.get(f"/api/v1/patient/bookings/{visit.id}/voice-notes", headers=auth_headers(patient.user)))
        stranger = make_patient(db, full_name="Placeholder Stranger")
        _save("34-patient-read-not-booker-404.json", client.get(f"/api/v1/patient/bookings/{visit.id}/voice-notes", headers=auth_headers(stranger.user)))
        clinical = make_admin(db, roles=("clinical_reviewer",), full_name="Placeholder Clinical Reviewer")
        _save("35-admin-read-200.json", client.get(f"/api/v1/admin/bookings/{visit.id}/voice-notes", headers=auth_headers(clinical)))
        finance = make_admin(db, roles=("finance",), full_name="Placeholder Finance")
        _save("36-admin-read-no-permission-403.json", client.get(f"/api/v1/admin/bookings/{visit.id}/voice-notes", headers=auth_headers(finance)))

        # --- limits and the switch ---
        monkeypatch.setattr(settings, "VOICE_NOTE_MAX_PER_CAREGIVER_PER_DAY", 1)
        capped = client.post(url, json=_body(client_request_id="66666666-2222-4333-8444-555555555555"), headers=_hdr(caregiver)).json()["id"]
        _save("37-audio-daily-limit-429.json", audio(capped))
        monkeypatch.setattr(settings, "VOICE_NOTE_MAX_PER_CAREGIVER_PER_DAY", 30)
        monkeypatch.setattr(settings, "VOICE_NOTE_MAX_PER_BOOKING", 1)
        _save("38-create-visit-limit-429.json", client.post(url, json=_body(client_request_id="77777777-2222-4333-8444-555555555555"), headers=_hdr(caregiver)))
        monkeypatch.setattr(settings, "VOICE_NOTE_MAX_PER_BOOKING", 20)
        monkeypatch.setattr(settings, "VOICE_NOTES_ENABLED", False)
        _save("39-create-switched-off-503.json", client.post(url, json=_body(client_request_id="88888888-2222-4333-8444-555555555555"), headers=_hdr(caregiver)))
        monkeypatch.setattr(settings, "VOICE_NOTES_ENABLED", True)

        # --- one more visit: after the visit, inside and outside the window ---
        done = make_booking(db, patient, caregiver, status=BookingStatus.COMPLETED, booking_for="myself",
                            actual_end_time=datetime.utcnow() - timedelta(minutes=30))
        _save("40-create-after-visit-inside-grace-201.json", client.post(f"{BASE}/visits/{done.id}/voice-notes", json=_body(client_request_id="99999999-2222-4333-8444-555555555555"), headers=_hdr(caregiver)))
    finally:
        fastapi_app.dependency_overrides.pop(get_voice_note_queue, None)
        audio_store.set_audio_store_for_tests(None)
        provider.set_provider_for_tests(None)
        (OUT / "000-index.json").write_text(json.dumps({"files": INDEX, "note": "Captured by app/tests/test_capture_voice_notes.py. All data is placeholder."}, indent=2) + "\n")
