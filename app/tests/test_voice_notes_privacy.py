"""Voice notes: nothing readable at rest, no audio or text in logs or audit rows (plan section 6.5 and 9). Plan tests 28-31."""
import logging

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.security import decrypt
from app.db.models.voice_note import VisitVoiceNote, VoiceNoteEvent
from app.tests.factories import auth_headers, make_admin
from app.tests.fake_gemini import SENTINELS, SENTINEL_SUMMARY, SENTINEL_TRANSCRIPT
from app.tests.voice_note_helpers import (
    BASE, create, get_note, h, ready_note, save, send_audio, visit, voice,  # noqa: F401
)


def _dump(db, table):
    return "".join(r[0] for r in db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")))


def test_28_nothing_readable_at_rest_and_rotation_reaches_the_columns(db, client, voice):
    from scripts.reencrypt_health_data import COLUMNS, reencrypt

    cg, _, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    assert get_note(client, cg, note_id).status_code == 200
    save(client, cg, note_id, transcript=SENTINEL_TRANSCRIPT, summary=SENTINEL_SUMMARY)  # saved text is encrypted too
    for table in ("visit_voice_notes", "voice_note_events"):
        dump = _dump(db, table)
        assert not any(s in dump for s in SENTINELS), table
    row = db.get(VisitVoiceNote, note_id)
    db.refresh(row)
    assert row.transcript_encrypted.startswith("gAAAAA") and decrypt(row.transcript_encrypted) == SENTINEL_TRANSCRIPT
    assert decrypt(row.summary_encrypted) == SENTINEL_SUMMARY

    from app.db.base import Base

    actual = {(t, c.name) for t in ("visit_voice_notes",) for c in Base.metadata.tables[t].columns if c.name.endswith("_encrypted")}
    assert actual == {(c.table, c.column) for c in COLUMNS if c.table == "visit_voice_notes"}
    report = reencrypt(dry_run=False)
    assert report["visit_voice_notes.transcript_encrypted"].rotated >= 1 and report["visit_voice_notes.transcript_encrypted"].unreadable == 0
    assert report["visit_voice_notes.summary_encrypted"].rotated >= 1
    db.expire_all()
    assert get_note(client, cg, note_id).json()["transcript"] == SENTINEL_TRANSCRIPT


def test_29_the_audio_is_encrypted_in_the_store_and_gone_when_it_should_be(db, client, voice):
    cg, _, booking = visit(db)
    note_id = create(client, cg, booking).json()["id"]
    send_audio(client, cg, note_id)
    assert voice.store.exists(note_id)
    assert all(b"SENTINEL-AUDIO" not in raw and raw.startswith(b"gAAAAA") for raw in voice.store.raw_values())
    voice.run_job(note_id)
    assert voice.store.list_ids() == []  # gone the moment the text exists

    discarded = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-0000000000d1").json()["id"]
    send_audio(client, cg, discarded)
    assert voice.store.exists(discarded)
    client.delete(f"{BASE}/voice-notes/{discarded}", headers=h(cg))
    assert not voice.store.exists(discarded)

    failed = create(client, cg, booking, client_request_id="00000000-0000-4000-8000-0000000000d2").json()["id"]
    send_audio(client, cg, failed)
    voice.gemini.fail_next("crash")
    voice.run_job(failed)
    assert voice.store.exists(failed)  # kept so retry works ...
    voice.clock.t += settings.VOICE_NOTE_AUDIO_TTL_SECONDS + 1
    assert not voice.store.exists(failed)  # ... and gone at its TTL


def test_30_no_audio_or_text_in_any_log_line(db, client, voice, caplog):
    caplog.set_level(logging.DEBUG)
    cg, patient, booking = visit(db)
    ids = []
    for i, failure in enumerate([None, "no_speech", "refused", "blocked", "transient", "crash", "timeout", "garbage"]):
        note_id = create(client, cg, booking, client_request_id=f"00000000-0000-4000-8000-0000000001{i:02d}").json()["id"]
        send_audio(client, cg, note_id)
        if failure:
            voice.gemini.fail_next(failure)
        voice.gemini.fail_next("crash", target="summarize") if failure is None else None
        voice.run_job(note_id)
        ids.append(note_id)
    good = ids[0]
    save(client, cg, good, transcript=SENTINEL_TRANSCRIPT, summary=SENTINEL_SUMMARY)
    client.get(f"/api/v1/patient/bookings/{booking.id}/voice-notes", headers=auth_headers(patient.user))
    client.get(f"/api/v1/admin/bookings/{booking.id}/voice-notes", headers=auth_headers(make_admin(db)))
    client.delete(f"{BASE}/voice-notes/{good}", headers=h(cg))
    assert caplog.records, "the run should have logged something at DEBUG for this to mean anything"
    formatter = logging.Formatter()
    for record in caplog.records:
        blob = record.getMessage() + repr(record.args) + (formatter.formatException(record.exc_info) if record.exc_info else "") + str(record.exc_text)
        for sentinel in SENTINELS:
            assert sentinel not in blob, f"{sentinel} in a log line from {record.name}"
        assert b"SENTINEL-AUDIO" not in blob.encode()


def test_31_audit_rows_hold_no_content_and_every_non_author_read_is_recorded(db, client, voice):
    cg, patient, booking = visit(db)
    note_id = ready_note(client, voice, cg, booking)
    save(client, cg, note_id)
    client.get(f"/api/v1/patient/bookings/{booking.id}/voice-notes", headers=auth_headers(patient.user))
    admin = make_admin(db)
    client.get(f"/api/v1/admin/bookings/{booking.id}/voice-notes", headers=auth_headers(admin))
    client.get(f"{BASE}/voice-notes/{note_id}", headers=h(cg))  # the author's own polling is not audited
    events = db.query(VoiceNoteEvent).filter_by(voice_note_id=note_id).all()
    reads = [(e.actor_type, str(e.actor_id)) for e in events if e.action == "read"]
    assert sorted(reads) == sorted([("patient", str(patient.user_id)), ("admin", str(admin.id))])
    assert {"created", "audio_received", "transcribed", "audio_deleted", "saved", "read"} <= {e.action for e in events}
    dump = _dump(db, "voice_note_events") + _dump(db, "admin_audit_log")
    assert not any(s in dump for s in SENTINELS)
    admin_rows = db.execute(text("SELECT action, entity_type FROM admin_audit_log WHERE action = 'voice_note.read'")).fetchall()
    assert admin_rows == [("voice_note.read", "booking")]
    # the events hold ids, counts, sizes and codes: check every detail value is that kind of thing
    for e in events:
        for value in (e.detail or {}).values():
            assert isinstance(value, (int, bool, str, type(None))) and (not isinstance(value, str) or len(value) < 40)
