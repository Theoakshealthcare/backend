"""Voice notes: the caregiver contract (plan section 3) and who may use it (section 7). Plan tests 1-15."""
import tempfile
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models.booking import BookingStatus
from app.db.models.voice_note import VisitVoiceNote, VoiceNoteEvent
from app.services.voice_notes.constants import REQUEST_ERRORS
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.fake_gemini import FAKE_AUDIO, SENTINEL_SUMMARY, SENTINEL_TRANSCRIPT
from app.tests.voice_note_helpers import (
    BASE, CONSENT_VERSION, code_of, create, create_body, get_note, h, ready_note, save, send_audio, visit, voice,  # noqa: F401
)

NOTE_FIELDS = {"id", "booking_id", "status", "language", "transcript", "summary", "summary_available", "edited", "error",
               "duration_seconds", "consent_version", "consent_confirmed_at", "created_at", "ready_at", "saved_at", "expires_at"}


# --- 1-3: create, consent, validation ---------------------------------------------------------------------------------

def test_01_create_shape_and_idempotency(db, client, voice):
    cg, _, booking = visit(db)
    first = create(client, cg, booking)
    assert first.status_code == 201
    body = first.json()
    assert set(body) == NOTE_FIELDS
    assert body["status"] == "awaiting_audio" and body["transcript"] is None and body["summary"] is None
    assert body["consent_version"] == CONSENT_VERSION and not body["consent_confirmed_at"].endswith("Z")
    again = create(client, cg, booking)
    assert again.status_code == 200 and again.json()["id"] == body["id"]
    other = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-00000000dddd")
    assert other.status_code == 201 and other.json()["id"] != body["id"]
    assert db.query(VisitVoiceNote).count() == 2


def test_02_consent_is_required_and_stored(db, client, voice):
    cg, _, booking = visit(db)
    r = create(client, cg, booking, consent_confirmed=False)
    assert r.status_code == 400 and code_of(r) == "consent_required"
    assert create(client, cg, booking, consent_version="2020-01-v0").json()["detail"]["code"] == "consent_version_unknown"
    body = create_body()
    del body["consent_confirmed"]
    assert client.post(f"{BASE}/visits/{booking.id}/voice-notes", json=body, headers=h(cg)).status_code == 422
    assert db.query(VisitVoiceNote).count() == 0
    ok = create(client, cg, booking)
    row = db.get(VisitVoiceNote, ok.json()["id"])
    assert row.consent_confirmed_at is not None and row.consent_version == CONSENT_VERSION


@pytest.mark.parametrize("seconds,expected", [(1, 422), (2, 201), (600, 201), (601, 422)])
def test_03_duration_limits(db, client, voice, seconds, expected):
    cg, _, booking = visit(db)
    assert create(client, cg, booking, duration_seconds=seconds).status_code == expected


def test_03_language_and_visit_state(db, client, voice):
    cg, _, booking = visit(db)
    assert create(client, cg, booking, language="fr").status_code == 422
    for status in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.PENDING):
        cg2, _, b2 = visit(db, status=status)
        r = create(client, cg2, b2)
        assert r.status_code == 409 and code_of(r) == "visit_not_active"
    # completed: inside and outside the 120-minute grace
    end = datetime.utcnow()
    cg3, _, inside = visit(db, status=BookingStatus.COMPLETED, actual_end_time=end - timedelta(minutes=119))
    assert create(client, cg3, inside).status_code == 201
    cg4, _, outside = visit(db, status=BookingStatus.COMPLETED, actual_end_time=end - timedelta(minutes=121))
    assert code_of(create(client, cg4, outside)) == "visit_not_active"


# --- 4-5: the audio ---------------------------------------------------------------------------------------------------

def test_04_audio_put_accepts_a_raw_body_and_enqueues_only_the_id(db, client, voice, monkeypatch):
    import starlette.datastructures
    import tempfile as tf

    def no_temp_file(*a, **k):
        raise AssertionError("the audio must never touch a temp file")

    monkeypatch.setattr(tf, "SpooledTemporaryFile", no_temp_file)
    monkeypatch.setattr(starlette.datastructures, "SpooledTemporaryFile", no_temp_file, raising=False)
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    big = FAKE_AUDIO + bytes(3 * 1024 * 1024)  # past Starlette's 1 MB spool threshold
    r = send_audio(client, cg, note_id, data=big)
    assert r.status_code == 202 and r.json()["status"] == "processing"
    assert voice.queue.ids == [note_id]
    row = db.get(VisitVoiceNote, note_id)
    assert row.audio_bytes == len(big) and row.audio_mime == "audio/mp4"
    assert voice.store.exists(note_id)


def test_04_audio_put_refusals(db, client, voice, monkeypatch):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    empty = send_audio(client, cg, note_id, data=b"")
    assert empty.status_code == 400 and code_of(empty) == "audio_empty"
    wrong_type = send_audio(client, cg, note_id, content_type="application/pdf")
    assert wrong_type.status_code == 415 and code_of(wrong_type) == "unsupported_audio_type"
    not_audio = send_audio(client, cg, note_id, data=b"%PDF-1.4 not really audio at all" + bytes(40))
    assert not_audio.status_code == 415 and code_of(not_audio) == "unsupported_audio_type"
    monkeypatch.setattr(settings, "VOICE_NOTE_MAX_BYTES", 500)
    too_big = send_audio(client, cg, note_id, data=FAKE_AUDIO + bytes(600))
    assert too_big.status_code == 413 and code_of(too_big) == "audio_too_large"

    def chunks():  # a chunked body: no Content-Length, counted as it arrives
        yield FAKE_AUDIO
        yield bytes(600)

    chunked = client.put(f"{BASE}/voice-notes/{note_id}/audio", content=chunks(), headers={**h(cg), "Content-Type": "audio/mp4"})
    assert chunked.status_code == 413 and code_of(chunked) == "audio_too_large"
    assert db.get(VisitVoiceNote, note_id).status == "awaiting_audio" and not voice.store.exists(note_id)
    assert voice.queue.ids == []


def test_05_consent_missing_is_refused_defensively():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.services.voice_notes.service import require_consent

    with pytest.raises(HTTPException) as refused:
        require_consent(SimpleNamespace(consent_confirmed_at=None))
    assert refused.value.status_code == 409 and refused.value.detail["code"] == "consent_missing"
    require_consent(SimpleNamespace(consent_confirmed_at=datetime.utcnow()))


# --- 6-10: state machine, save, delete, list, headers ------------------------------------------------------------------

def test_06_state_machine_pairs(db, client, voice):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    assert code_of(save(client, cg, note_id)) == "not_ready"  # awaiting_audio
    assert code_of(client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg))) == "invalid_state"
    assert send_audio(client, cg, note_id).status_code == 202
    second = send_audio(client, cg, note_id)
    assert second.status_code == 409 and code_of(second) == "already_processing"
    assert voice.queue.ids == [note_id]  # still one job
    assert code_of(save(client, cg, note_id)) == "not_ready"  # processing
    assert voice.run_job(note_id).kind == "ready"
    ready = get_note(client, cg, note_id).json()
    assert ready["status"] == "ready" and ready["transcript"] == SENTINEL_TRANSCRIPT and ready["summary"] == SENTINEL_SUMMARY
    assert code_of(send_audio(client, cg, note_id)) == "invalid_state"  # ready
    assert code_of(client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg))) == "invalid_state"
    saved = save(client, cg, note_id)
    assert saved.status_code == 200 and saved.json()["status"] == "saved"
    assert code_of(send_audio(client, cg, note_id)) == "invalid_state"  # saved


def test_07_save_rules(db, client, voice):
    cg, _, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    draft = get_note(client, cg, note_id).json()
    unchanged = save(client, cg, note_id, transcript=draft["transcript"], summary=draft["summary"])
    assert unchanged.json()["edited"] is False
    # a second note: edited text
    other = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-00000000eeee")
    saved = save(client, cg, other, transcript="  Corrected text.  ", summary="")
    body = saved.json()
    assert body["status"] == "saved" and body["edited"] is True and body["expires_at"] is None and body["summary"] == ""
    assert body["summary_available"] is True and body["saved_at"] is not None
    row = db.get(VisitVoiceNote, other)
    assert row.retain_until is not None and row.retain_until > row.saved_at
    assert save(client, cg, other, transcript="Corrected text.", summary="").status_code == 200  # same text again
    different = save(client, cg, other, transcript="Something else.", summary="")
    assert different.status_code == 409 and code_of(different) == "already_saved"


def test_07_save_limits_are_validation_errors_that_do_not_echo_the_text(db, client, voice):
    cg, _, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    secret = "Placeholder secret text zeta " * 1000
    r = client.put(f"{BASE}/voice-notes/{note_id}", json={"transcript": secret, "summary": ""}, headers=h(cg))
    assert r.status_code == 422 and "Placeholder secret" not in r.text
    assert save(client, cg, note_id, transcript="   ").status_code == 422
    assert client.put(f"{BASE}/voice-notes/{note_id}", json={"transcript": "ok", "summary": "s" * 2001}, headers=h(cg)).status_code == 422


def test_07_save_before_ready_for_a_failed_note(db, client, voice):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    send_audio(client, cg, note_id)
    voice.gemini.fail_next("no_speech")
    voice.run_job(note_id)
    assert get_note(client, cg, note_id).json()["status"] == "failed"
    assert code_of(save(client, cg, note_id)) == "not_ready"


def test_08_discard_and_delete(db, client, voice):
    cg, _, booking = visit(db)
    # each state
    awaiting = create(client, cg, booking).json()["id"]
    processing = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-000000000001").json()["id"]
    send_audio(client, cg, processing)
    ready = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-000000000002")
    saved = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-000000000003")
    save(client, cg, saved)
    for note_id in (awaiting, processing, ready, saved):
        assert client.delete(f"{BASE}/voice-notes/{note_id}", headers=h(cg)).status_code == 204
        assert db.query(VisitVoiceNote).filter_by(id=note_id).count() == 0
        assert not voice.store.exists(note_id)
        second = client.delete(f"{BASE}/voice-notes/{note_id}", headers=h(cg))
        assert second.status_code == 404 and code_of(second) == "not_found"
    actions = [e.action for e in db.query(VoiceNoteEvent).order_by(VoiceNoteEvent.created_at)]
    assert actions.count("discarded") == 3 and actions.count("deleted") == 1


def test_08_a_saved_note_outside_the_edit_window_cannot_be_deleted(db, client, voice, monkeypatch):
    import app.api.v1.endpoints.caregiver_voice_notes as endpoint

    cg, _, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    save(client, cg, note_id)
    booking.status = BookingStatus.COMPLETED
    booking.actual_end_time = datetime.utcnow()
    db.commit()
    monkeypatch.setattr(endpoint, "_now", lambda: datetime.utcnow() + timedelta(hours=25))
    r = client.delete(f"{BASE}/voice-notes/{note_id}", headers=h(cg))
    assert r.status_code == 404 and code_of(r) == "not_found"
    assert db.query(VisitVoiceNote).filter_by(id=note_id).count() == 1


def test_09_poll_and_list_are_the_callers_own_newest_first(db, client, voice):
    cg, _, booking = visit(db)
    one = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-0000000000a1").json()["id"]
    two = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-0000000000a2").json()["id"]
    items = client.get(f"{BASE}/visits/{booking.id}/voice-notes", headers=h(cg)).json()["items"]
    assert [i["id"] for i in items] == [two, one]
    # another caregiver's note on the same booking is not in the list (and cannot be polled)
    other = make_caregiver(db)
    assert get_note(client, other, one).status_code == 404
    assert client.get(f"{BASE}/visits/{booking.id}/voice-notes", headers=h(other)).status_code == 404


def test_10_every_route_is_no_store_and_every_error_has_a_code(db, client, voice):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    responses = [
        create(client, cg, booking), get_note(client, cg, note_id), save(client, cg, note_id),
        client.get(f"{BASE}/visits/{booking.id}/voice-notes", headers=h(cg)), send_audio(client, cg, note_id),
        client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg)),
        get_note(client, cg, "00000000-0000-4000-8000-000000000999"),
        client.delete(f"{BASE}/voice-notes/{note_id}", headers=h(cg)),
    ]
    for r in responses:
        assert r.headers["cache-control"] == "no-store", r.request.url
        if r.status_code >= 400:
            detail = r.json()["detail"]
            assert detail["code"] in REQUEST_ERRORS and detail["message"] == REQUEST_ERRORS[detail["code"]]


# --- 11-15: access -----------------------------------------------------------------------------------------------------

def test_11_no_access_is_one_404_whether_or_not_the_note_exists(db, client, voice):
    cg, patient, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    stranger = make_caregiver(db)
    suspended = make_caregiver(db, approved=False)
    on_other = make_booking(db, make_patient(db), stranger, status=BookingStatus.IN_PROGRESS)
    inactive_cg, inactive_patient, inactive_booking = visit(db)
    inactive_patient.is_active = False
    db.commit()
    missing = "00000000-0000-4000-8000-000000000999"
    answers = []
    for caregiver, target in ((stranger, note_id), (stranger, missing), (suspended, note_id)):
        r = get_note(client, caregiver, target)
        answers.append((r.status_code, r.json()))
    r = create(client, stranger, booking)  # not their visit
    answers.append((r.status_code, r.json()))
    r = create(client, inactive_cg, inactive_booking)  # inactive patient
    answers.append((r.status_code, r.json()))
    assert all(a == (404, {"detail": {"code": "not_found", "message": REQUEST_ERRORS["not_found"]}}) for a in answers)
    assert on_other is not None


def test_11_a_suspended_caregiver_keeps_only_a_visit_in_progress_and_reassignment_ends_access(db, client, voice):
    cg, _, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    cg.profile_status = "suspended"
    db.commit()
    assert get_note(client, cg, note_id).status_code == 200  # still in progress
    booking.status = BookingStatus.EN_ROUTE
    db.commit()
    assert get_note(client, cg, note_id).status_code == 404
    cg.profile_status = "approved"
    booking.caregiver_id = make_caregiver(db).id  # reassigned
    booking.status = BookingStatus.IN_PROGRESS
    db.commit()
    assert get_note(client, cg, note_id).status_code == 404


def test_12_caregiver_access_ends_24_hours_after_the_visit_the_booker_does_not(db, client, voice, monkeypatch):
    import app.api.v1.endpoints.caregiver_voice_notes as endpoint

    cg, patient, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    save(client, cg, note_id)
    booking.status = BookingStatus.COMPLETED
    booking.actual_end_time = datetime.utcnow()
    db.commit()
    assert get_note(client, cg, note_id).status_code == 200  # inside the window
    monkeypatch.setattr(endpoint, "_now", lambda: datetime.utcnow() + timedelta(hours=23))
    assert get_note(client, cg, note_id).status_code == 200
    monkeypatch.setattr(endpoint, "_now", lambda: datetime.utcnow() + timedelta(hours=25))
    assert get_note(client, cg, note_id).status_code == 404
    r = client.get(f"/api/v1/patient/bookings/{booking.id}/voice-notes", headers=auth_headers(patient.user))
    assert r.status_code == 200 and len(r.json()["items"]) == 1


def test_13_the_booker_sees_saved_notes_only(db, client, voice):
    cg, patient, booking = visit(db)
    draft = ready_note(client, voice, cg, booking)
    saved = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-0000000000b1")
    save(client, cg, saved)
    r = client.get(f"/api/v1/patient/bookings/{booking.id}/voice-notes", headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    items = r.json()["items"]
    assert [i["id"] for i in items] == [saved] and draft not in r.text
    assert set(items[0]) == {"id", "booking_id", "status", "language", "transcript", "summary", "summary_available", "edited",
                             "duration_seconds", "created_at", "ready_at", "saved_at"}
    stranger = make_patient(db)
    gone = client.get(f"/api/v1/patient/bookings/{booking.id}/voice-notes", headers=auth_headers(stranger.user))
    assert gone.status_code == 404 and code_of(gone) == "not_found"


def test_13_a_whatsapp_booking_is_read_by_the_booker(db, client, voice):
    cg = make_caregiver(db)
    booker = make_patient(db, full_name="Placeholder Booker")
    booking = make_booking(db, booker, cg, status=BookingStatus.IN_PROGRESS, booking_for="mother", channel="whatsapp",
                           booked_by_user_id=booker.user_id)
    note_id = ready_note(client, voice, cg, booking)
    save(client, cg, note_id)
    r = client.get(f"/api/v1/patient/bookings/{booking.id}/voice-notes", headers=auth_headers(booker.user))
    assert r.status_code == 200 and [i["id"] for i in r.json()["items"]] == [note_id]


def test_14_admin_reads_need_clinical_read_and_no_new_permission_exists(db, client, voice):
    from app.core.admin_permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS

    cg, _, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    save(client, cg, note_id)
    url = f"/api/v1/admin/bookings/{booking.id}/voice-notes"
    clinical = make_admin(db, roles=("clinical_reviewer",))
    ok = client.get(url, headers=auth_headers(clinical))
    assert ok.status_code == 200 and [i["id"] for i in ok.json()["items"]] == [note_id]
    for role in ("finance", "support"):
        assert client.get(url, headers=auth_headers(make_admin(db, roles=(role,)))).status_code == 403
    assert client.get(url).status_code in (401, 403)
    assert not any("voice" in p for p in ALL_PERMISSIONS) and "clinical.read" in ROLE_PERMISSIONS["clinical_reviewer"]


def test_15_drafts_never_reach_the_booker_or_an_admin(db, client, voice):
    cg, patient, booking = visit(db)
    ready_note(client, voice, cg, booking)  # ready, not saved
    create(client, cg, booking, client_request_id="00000000-0000-4000-8000-0000000000c1")  # awaiting
    assert client.get(f"/api/v1/patient/bookings/{booking.id}/voice-notes", headers=auth_headers(patient.user)).json() == {"items": []}
    admin = make_admin(db)
    assert client.get(f"/api/v1/admin/bookings/{booking.id}/voice-notes", headers=auth_headers(admin)).json() == {"items": []}
