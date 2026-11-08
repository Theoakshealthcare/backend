"""Voice notes: limits, the kill switch, the sweeper, erasure, the migration and the settings (plan sections 8, 10). Plan tests 33-37."""
from datetime import datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, settings
from app.db.models.booking import BookingStatus
from app.db.models.voice_note import VisitVoiceNote, VoiceNoteEvent
from app.services.account_erasure import AccountErasureService
from app.services.voice_notes.processing import sweep
from app.tests.factories import make_booking, make_caregiver, make_patient
from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)
from app.tests.voice_note_helpers import (
    BASE, CONSENT_VERSION, code_of, create, get_note, h, ready_note, save, send_audio, visit, voice,  # noqa: F401
)


# --- 33: limits and the kill switch ------------------------------------------------------------------------------------

def test_33_per_visit_limit(db, client, voice, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_NOTE_MAX_PER_BOOKING", 2)
    cg, _, booking = visit(db)
    for i in range(2):
        assert create(client, cg, booking, client_request_id=f"00000000-0000-4000-8000-000000000a0{i}").status_code == 201
    r = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-000000000a09")
    assert r.status_code == 429 and code_of(r) == "note_limit_reached"
    assert create(client, cg, booking, client_request_id="00000000-0000-4000-8000-000000000a00").status_code == 200  # a repeat is not a new note


def test_33_per_caregiver_daily_limit_counts_at_the_audio_not_the_create_and_survives_a_discard(db, client, voice, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_NOTE_MAX_PER_CAREGIVER_PER_DAY", 1)
    cg, _, booking = visit(db)
    ids = [create(client, cg, booking, client_request_id=f"00000000-0000-4000-8000-000000000b0{i}").json()["id"] for i in range(3)]
    assert send_audio(client, cg, ids[0]).status_code == 202  # creating three notes spent nothing
    r = send_audio(client, cg, ids[1])
    assert r.status_code == 429 and code_of(r) == "daily_limit_reached" and int(r.headers["retry-after"]) > 0
    client.delete(f"{BASE}/voice-notes/{ids[0]}", headers=h(cg))  # discarding does not give the day back
    assert code_of(send_audio(client, cg, ids[1])) == "daily_limit_reached"
    assert db.query(VisitVoiceNote).filter_by(id=ids[1]).one().status == "awaiting_audio"
    assert not voice.store.exists(ids[1])


def test_33_global_daily_audio_budget(db, client, voice, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_NOTE_DAILY_AUDIO_SECONDS_CAP", 100)
    cg, _, booking = visit(db)
    first = create(client, cg, booking, duration_seconds=60, client_request_id="00000000-0000-4000-8000-000000000c01").json()["id"]
    second = create(client, cg, booking, duration_seconds=60, client_request_id="00000000-0000-4000-8000-000000000c02").json()["id"]
    assert send_audio(client, cg, first).status_code == 202
    r = send_audio(client, cg, second)  # 60 + 60 > 100 for the day, across every caregiver
    assert r.status_code == 503 and code_of(r) == "transcription_unavailable"
    other_cg, _, other_booking = visit(db)
    third = create(client, other_cg, other_booking, duration_seconds=60).json()["id"]
    assert code_of(send_audio(client, other_cg, third)) == "transcription_unavailable"


def test_33_the_kill_switch_stops_new_work_but_not_reads_or_deletes(db, client, voice, monkeypatch):
    cg, _, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    other = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-000000000d01").json()["id"]
    monkeypatch.setattr(settings, "VOICE_NOTES_ENABLED", False)
    r = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-000000000d02")
    assert r.status_code == 503 and code_of(r) == "transcription_unavailable"
    assert code_of(send_audio(client, cg, other)) == "transcription_unavailable"
    assert get_note(client, cg, note_id).status_code == 200
    assert client.get(f"{BASE}/visits/{booking.id}/voice-notes", headers=h(cg)).status_code == 200
    assert save(client, cg, note_id).status_code == 200
    assert client.delete(f"{BASE}/voice-notes/{note_id}", headers=h(cg)).status_code == 204


def test_a_down_audio_store_commits_nothing_and_a_down_queue_fails_the_note_as_retryable(db, client, voice):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]

    def broken_put(*args, **kwargs):
        raise ConnectionError("redis down")

    original = voice.store.put
    voice.store.put = broken_put
    r = send_audio(client, cg, note_id)
    assert r.status_code == 503 and code_of(r) == "transcription_unavailable"
    assert db.query(VisitVoiceNote).filter_by(id=note_id).one().status == "awaiting_audio"
    voice.store.put = original
    voice.queue.fail = True
    r = send_audio(client, cg, note_id)
    assert r.status_code == 503
    db.expire_all()
    row = db.query(VisitVoiceNote).filter_by(id=note_id).one()
    assert row.status == "failed" and row.error_code == "provider_unavailable"
    voice.queue.fail = False
    assert client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg)).status_code == 202  # the audio is still parked


# --- 34: the sweeper ---------------------------------------------------------------------------------------------------

def _age(db, note_id, **cols):
    db.query(VisitVoiceNote).filter_by(id=note_id).update(cols)
    db.commit()


def test_34_sweeper_rules(db, client, voice):
    now = datetime.utcnow()
    cg, _, booking = visit(db)
    ids = {}

    def new(name):
        ids[name] = create(client, cg, booking, client_request_id=f"00000000-0000-4000-8000-0000000e{len(ids):04d}").json()["id"]
        return ids[name]

    awaiting_old, awaiting_new = new("awaiting_old"), new("awaiting_new")
    _age(db, awaiting_old, updated_at=now - timedelta(hours=2))
    stuck, working = new("stuck"), new("working")
    for n in (stuck, working):
        send_audio(client, cg, n)
    _age(db, stuck, updated_at=now - timedelta(minutes=6), claimed_at=None)
    _age(db, working, updated_at=now - timedelta(minutes=6), claimed_at=now - timedelta(seconds=30))  # a worker is on it
    draft = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-0000000f0001")
    _age(db, draft, expires_at=now - timedelta(minutes=1))
    fresh_draft = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-0000000f0002")
    old_saved = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-0000000f0003")
    save(client, cg, old_saved)
    _age(db, old_saved, retain_until=now - timedelta(days=1))
    kept_saved = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-0000000f0004")
    save(client, cg, kept_saved)
    orphan = "00000000-0000-4000-8000-0000000a9999"
    voice.store.put(orphan, b"gAAAAA-orphan", 600)

    counts = sweep(db, voice.store, now)
    assert counts == {"never_uploaded": 1, "stuck": 1, "draft_expired": 1, "retention": 1, "caregiver_changed": 0, "audio_orphans": 1}
    db.expire_all()
    remaining = {str(r.id): r for r in db.query(VisitVoiceNote)}
    assert awaiting_old not in remaining and draft not in remaining and old_saved not in remaining
    assert {awaiting_new, stuck, working, fresh_draft, kept_saved} <= set(remaining)
    assert remaining[stuck].status == "failed" and remaining[stuck].error_code == "transcription_timeout"
    assert remaining[working].status == "processing"  # a note being processed is not touched
    assert voice.store.exists(stuck) and not voice.store.exists(orphan)
    reasons = sorted(e.detail["reason"] for e in db.query(VoiceNoteEvent).filter_by(action="purged"))
    assert reasons == ["draft_expired", "never_uploaded", "retention"]
    assert sweep(db, voice.store, now) == {"never_uploaded": 0, "stuck": 0, "draft_expired": 0, "retention": 0, "caregiver_changed": 0, "audio_orphans": 0}


def test_34_a_reassigned_booking_loses_the_old_caregivers_drafts_not_their_saved_notes(db, client, voice):
    cg, _, booking = visit(db)
    draft = ready_note(client, voice, cg, booking)
    saved = ready_note(client, voice, cg, booking, client_request_id="00000000-0000-4000-8000-0000000f0010")
    save(client, cg, saved)
    booking.caregiver_id = make_caregiver(db).id
    db.commit()
    assert sweep(db, voice.store)["caregiver_changed"] == 1
    db.expire_all()
    assert {str(r.id) for r in db.query(VisitVoiceNote)} == {saved}
    assert draft and db.query(VoiceNoteEvent).filter_by(action="purged").one().detail == {"reason": "caregiver_changed"}


# --- 35: erasure ---------------------------------------------------------------------------------------------------------

def test_35_erasing_a_patient_deletes_their_voice_notes_and_parked_audio(db, client, voice):
    cg, patient, booking = visit(db)
    saved = ready_note(client, voice, cg, booking)
    save(client, cg, saved)
    parked = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-0000000f0020").json()["id"]
    send_audio(client, cg, parked)
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(patient.user_id)
    db.commit()
    assert db.query(VisitVoiceNote).count() == 0 and voice.store.list_ids() == []
    events = db.query(VoiceNoteEvent).filter_by(action="purged").all()
    assert len(events) == 2 and all(e.detail == {"reason": "account_erased"} for e in events)
    assert db.query(VoiceNoteEvent).filter_by(action="created").count() == 2  # the trail stays


def test_35_erasing_a_caregiver_keeps_the_notes_they_wrote(db, client, voice):
    cg, patient, booking = visit(db)
    saved = ready_note(client, voice, cg, booking)
    save(client, cg, saved)
    booking.status = BookingStatus.COMPLETED
    db.commit()
    AccountErasureService(db).erase(cg.user_id)
    db.commit()
    assert db.query(VisitVoiceNote).filter_by(id=saved).count() == 1


# --- 36: the migration -------------------------------------------------------------------------------------------------

THIS, PREVIOUS = "0025_visit_voice_notes", "0024_arrival_policy_cancel_note"


def test_36_one_head_and_this_follows_its_predecessor():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert len(script.get_heads()) == 1
    assert script.get_revision(THIS).down_revision == PREVIOUS


def test_36_round_trip(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    assert {"visit_voice_notes", "voice_note_events"} <= set(inspect(scratch).get_table_names())
    _run(scratch, command.downgrade, PREVIOUS)
    assert not {"visit_voice_notes", "voice_note_events"} & set(inspect(scratch).get_table_names())
    _run(scratch, command.upgrade, THIS)
    assert {"visit_voice_notes", "voice_note_events"} <= set(inspect(scratch).get_table_names())


def _row(booking, caregiver, **over):
    fields = dict(booking_id=booking.id, caregiver_id=caregiver.id, language="en", duration_seconds=10,
                  consent_confirmed_at=datetime.utcnow(), consent_version=CONSENT_VERSION)
    fields.update(over)
    return VisitVoiceNote(**fields)


@pytest.mark.parametrize("over", [{"status": "nonsense"}, {"language": "fr"}, {"duration_seconds": 1}, {"duration_seconds": 601}])
def test_36_constraints_refuse_bad_rows(db, over):
    cg, _, booking = visit(db)
    db.add(_row(booking, cg, **over))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_36_a_request_id_is_unique_per_booking(db):
    import uuid

    cg, _, booking = visit(db)
    rid = uuid.uuid4()
    db.add(_row(booking, cg, client_request_id=rid))
    db.commit()
    db.add(_row(booking, cg, client_request_id=rid))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(_row(booking, cg))  # no key: any number
    db.add(_row(booking, cg))
    db.commit()


def test_36_events_have_no_foreign_key_to_the_note(db):
    fks = inspect(db.get_bind()).get_foreign_keys("voice_note_events")
    assert [fk["referred_table"] for fk in fks] == ["bookings"]


# --- 37: settings --------------------------------------------------------------------------------------------------------

def test_37_the_new_settings_have_defaults_and_are_in_env_example():
    from pathlib import Path

    names = [n for n in Settings.model_fields if n.startswith("VOICE_NOTE")]
    assert "VOICE_NOTES_ENABLED" in names and len(names) == 15
    assert all(not Settings.model_fields[n].is_required() for n in names)
    assert Settings.model_fields["VOICE_NOTES_ENABLED"].default is False  # off until the owner switches it on
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    for name in names:
        assert f"{name}=" in example, name
    assert Settings.model_config.get("extra") == "forbid"


# --- fixes from the privacy review -----------------------------------------------------------------------------------

def test_a_retry_counts_against_the_caregivers_day_and_the_budget(db, client, voice, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_NOTE_MAX_PER_CAREGIVER_PER_DAY", 3)
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    send_audio(client, cg, note_id)  # spends 1
    voice.gemini.fail_next("transient", times=3)
    for _ in range(3):
        voice.run_job(note_id)
    assert client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg)).status_code == 202  # spends 2
    voice.gemini.fail_next("transient", times=3)
    for _ in range(3):
        voice.run_job(note_id)
    assert client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg)).status_code == 202  # spends 3
    voice.gemini.fail_next("transient", times=3)
    for _ in range(3):
        voice.run_job(note_id)
    r = client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg))
    assert r.status_code == 429 and code_of(r) == "daily_limit_reached"
    assert db.query(VisitVoiceNote).filter_by(id=note_id).one().status == "failed"


def test_the_budget_is_charged_from_the_bytes_not_only_the_claim(db, client, voice, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_NOTE_DAILY_AUDIO_SECONDS_CAP", 20)
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking, duration_seconds=2).json()["id"]  # claims 2 seconds ...
    from app.tests.fake_gemini import FAKE_AUDIO

    r = send_audio(client, cg, note_id, data=FAKE_AUDIO + bytes(480_000))  # ... but the body is about 30 seconds of audio
    assert r.status_code == 503 and code_of(r) == "transcription_unavailable"
    assert not voice.store.exists(note_id)


def test_the_parked_audio_is_extended_once_not_on_every_failure(db, client, voice):
    from app.services.voice_notes.constants import STATUS_FAILED  # noqa: F401

    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    send_audio(client, cg, note_id)
    voice.gemini.fail_next("crash")
    voice.run_job(note_id)  # first failure: the TTL is refreshed to a full window from now
    voice.clock.t += settings.VOICE_NOTE_AUDIO_TTL_SECONDS - 100
    assert client.post(f"{BASE}/voice-notes/{note_id}/retry", headers=h(cg)).status_code == 202
    voice.gemini.fail_next("crash")
    voice.run_job(note_id)  # second failure: NOT extended again
    voice.clock.t += 150
    assert not voice.store.exists(note_id)  # it expired at the first window's end


def test_turning_the_switch_off_stops_queued_jobs_from_sending_audio(db, client, voice, monkeypatch):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    send_audio(client, cg, note_id)
    monkeypatch.setattr(settings, "VOICE_NOTES_ENABLED", False)
    assert voice.run_job(note_id).kind == "failed"
    assert voice.gemini.calls == []
    assert db.query(VisitVoiceNote).filter_by(id=note_id).one().error_code == "provider_unavailable"
    assert voice.store.exists(note_id)  # kept: switched back on, a retry works


def test_the_sweeper_does_not_delete_audio_that_is_being_uploaded(db, client, voice):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]  # still awaiting_audio ...
    voice.store.put(note_id, b"gAAAAA-just-parked", 600)  # ... with audio parked a moment before its status is committed
    assert sweep(db, voice.store)["audio_orphans"] == 0
    assert voice.store.exists(note_id)
