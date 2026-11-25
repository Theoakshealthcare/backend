"""Writes the live captures behind docs/fe-integration-guide-post-visit-windows-location.md.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_POST_VISIT_WINDOWS=1 poetry run pytest app/tests/test_capture_post_visit_windows.py -q

Expo is the fake transport (a push never leaves the machine): the nudge capture is the exact request body the app sends to Expo,
as recorded by the fake. The nudge job is called directly with an explicit ``now``; the ARQ cron schedule is not exercised.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus, CaregiverLocation
from app.db.models.push_token import PushToken
from app.services.location_nudge import nudge_stale_locations
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_POST_VISIT_WINDOWS"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "post-visit-windows-location"
BOOKING = "/api/v1/booking"
CHAT_KEYS = ("id", "status", "chat_open", "chat_closes_at", "notes_open", "notes_closes_at", "notes_readable_until")


def _write(name, doc):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _http(r, method="GET", body=None):
    return {
        "request": {"method": method, "path": r.request.url.path, "auth": "Bearer <ACCESS_TOKEN_JWT>", "body": body},
        "response": {"status_code": r.status_code, "body": r.json() if r.content else None},
    }


def _completed(db, patient, cg, hours_ago):
    end = datetime.utcnow() - timedelta(hours=hours_ago)
    return make_booking(db, patient, cg, status=BookingStatus.COMPLETED, actual_end_time=end,
                        scheduled_date=end - timedelta(hours=4), scheduled_end_date=end)


def test_capture(db, client, expo):
    caregiver = make_caregiver(db, full_name="Placeholder Chidi Nwosu")
    patient = make_patient(db, full_name="Placeholder Ada Okafor")
    c, p = auth_headers(caregiver.user), auth_headers(patient.user)

    recent = _completed(db, patient, caregiver, hours_ago=2)  # chat open, notes grace over
    just_done = _completed(db, patient, caregiver, hours_ago=1)  # notes still open
    old = _completed(db, patient, caregiver, hours_ago=30)  # chat closed
    cancelled = make_booking(db, patient, caregiver, status=BookingStatus.CANCELLED)
    live = make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS)

    # 1. The caregiver's chat list carries chat_open / chat_closes_at.
    r = client.get("/api/v1/caregiver/chats", headers=c)
    assert r.status_code == 200
    _write("001-c-chats-list-chat-open.json", _http(r))

    # 2. The patient's booking detail (the chat screen reads it): open, closed, cancelled.
    for name, booking in (("002-p-booking-detail-chat-open", recent), ("003-p-booking-detail-chat-closed", old),
                          ("004-p-booking-detail-cancelled", cancelled)):
        r = client.get(f"{BOOKING}/booking/{booking.id}", headers=p)
        assert r.status_code == 200
        doc = _http(r)
        doc["response"]["body"] = {k: doc["response"]["body"][k] for k in CHAT_KEYS}
        doc["note"] = "Only the chat/notes fields of the booking payload are kept (the rest is unchanged)."
        _write(f"{name}.json", doc)

    # 3. Sending to a closed chat, by each person; and to a cancelled booking.
    body = {"message": "Placeholder: are you still there?"}
    r = client.post(f"{BOOKING}/chat/{old.id}/send", headers=p, json=body)
    assert r.status_code == 409
    _write("005-p-chat-send-closed.json", _http(r, "POST", body))
    r = client.post(f"{BOOKING}/chat/{old.id}/send", headers=c, json=body)
    assert r.status_code == 409
    _write("006-c-chat-send-closed.json", _http(r, "POST", body))
    r = client.post(f"{BOOKING}/chat/{cancelled.id}/send", headers=p, json=body)
    assert r.status_code == 409
    _write("007-p-chat-send-cancelled.json", _http(r, "POST", body))

    # 4. History is still readable once closed.
    from app.db.models.booking import ChatMessage
    db.add(ChatMessage(booking_id=old.id, sender_id=caregiver.user_id, message="Placeholder: see you next week.",
                       created_at=datetime.utcnow() - timedelta(hours=31)))
    db.commit()
    r = client.get(f"{BOOKING}/chat/{old.id}/messages", headers=p)
    assert r.status_code == 200 and len(r.json()) == 1
    _write("008-p-chat-history-closed.json", _http(r))

    # 5. The caregiver's booking payloads carry the notes window.
    r = client.get(f"{BOOKING}/caregiver/bookings", headers=c)
    assert r.status_code == 200
    rows = {b["id"]: b for b in r.json()}
    doc = _http(r)
    doc["response"]["body"] = [{k: rows[str(b.id)][k] for k in CHAT_KEYS} for b in (live, just_done, recent, old)]
    doc["note"] = "In order: in progress, completed 1 h ago, completed 2 h ago, completed 30 h ago (chat/notes fields only)."
    _write("009-c-bookings-notes-window.json", doc)

    # 6. Typed notes: allowed in the window, refused outside it, readable for 24 h.
    note = {"title": "Placeholder visit note", "content": "Placeholder: all well."}
    r = client.post(f"{BOOKING}/notes/{just_done.id}/create", headers=c, json=note)
    assert r.status_code == 200
    _write("010-c-note-create-in-window.json", _http(r, "POST", note))
    r = client.post(f"{BOOKING}/notes/{recent.id}/create", headers=c, json=note)
    assert r.status_code == 409
    _write("011-c-note-create-after-grace.json", _http(r, "POST", note))
    r = client.get(f"{BOOKING}/notes/{old.id}", headers=c)
    assert r.status_code == 404
    _write("012-c-notes-read-after-24h.json", _http(r))

    # 7. The stale-location nudge: the request body the app sends to Expo (fake Expo; the cron schedule is not run).
    now = datetime(2026, 11, 25, 12, 0)  # 13:00 in Lagos
    idle = make_caregiver(db, full_name="Placeholder Amaka Eze")  # the first caregiver has a visit in progress: never nudged
    db.add(CaregiverLocation(caregiver_id=idle.id, latitude=6.45, longitude=3.47, is_active=True, is_available=True,
                             updated_at=now - timedelta(hours=7)))
    db.add(PushToken(user_id=idle.user_id, token="ExponentPushToken[placeholder-caregiver-device]", platform="ios"))
    db.commit()
    assert nudge_stale_locations(db, now) == 1
    _write("013-nudge-push-to-expo.json", {"observed": expo.batches[-1], "note": "The request body the app made to Expo for "
           "one caregiver whose last fix is 7 h old (fake Expo; nudge_stale_locations called with an explicit now)."})
