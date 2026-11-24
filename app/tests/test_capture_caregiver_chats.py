"""Writes the live captures behind the Chats list section of the caregiver FE guide.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_CAREGIVER_CHATS=1 poetry run pytest app/tests/test_capture_caregiver_chats.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver.md docs/fe-captures/caregiver`.
It only writes the 25x files. scripts/capture_caregiver_flow.py (compose stack) rewrites the whole directory, so
fold these calls into it the next time it is run (the same arrangement as test_capture_client_profile.py).
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db.models.booking import BookingNotification, BookingStatus, ChatMessage
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CAREGIVER_CHATS"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "caregiver"
URL = "/api/v1/caregiver/chats"


def _save(name, r, params=None, auth=True, note=None):
    doc = {
        "request": {"method": "GET", "path": r.request.url.path, "query": params or None,
                    "auth": "Bearer <ACCESS_TOKEN_JWT>" if auth else None, "body": None},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control")},
                     "body": r.json()},
    }
    if note:
        doc["note"] = note
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _say(db, booking, sender, text, minutes_ago):
    db.add(ChatMessage(booking_id=booking.id, sender_id=sender.id, message=text,
                       created_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
    db.commit()


def test_capture(db, client):
    caregiver, other = make_caregiver(db), make_caregiver(db)
    headers = auth_headers(caregiver.user)

    _save("250-a-chats-empty.json", client.get(URL, headers=headers), note="A caregiver with no assigned booking yet.")

    ada = make_patient(db, full_name="Placeholder Ada")
    ben = make_patient(db, full_name="Placeholder Ben Okoro")
    cara = make_patient(db, full_name="Placeholder Cara")
    dan = make_patient(db, full_name="Placeholder Dan")
    b_ada = make_booking(db, ada, caregiver, status=BookingStatus.IN_PROGRESS, service_type="nurse")
    b_ben = make_booking(db, ben, caregiver, status=BookingStatus.ACCEPTED, service_type="companion")
    b_cara = make_booking(db, cara, caregiver, status=BookingStatus.ACCEPTED, service_type="nurse")
    b_dan = make_booking(db, dan, caregiver, status=BookingStatus.COMPLETED, service_type="nurse")
    _say(db, b_ada, ada.user, "Placeholder: good morning, are you close?", 30)
    _say(db, b_ada, caregiver.user, "Placeholder: yes, five minutes away.", 25)
    _say(db, b_ben, ben.user, "Placeholder: " + "a very long message " * 12, 12)
    _say(db, b_dan, caregiver.user, "Placeholder: all done, take care.", 600)
    # b_cara has no message yet: last_message is null.
    offered = make_booking(db, make_patient(db, full_name="Placeholder Offered"), None, status=BookingStatus.PENDING)
    db.add(BookingNotification(booking_id=offered.id, caregiver_id=caregiver.id, title="New Booking Request",
                               message="Placeholder", notification_type="new_booking"))
    theirs = make_booking(db, make_patient(db, full_name="Placeholder Other"), other, status=BookingStatus.ACCEPTED)
    _say(db, theirs, theirs.patient.user, "Placeholder: a message for another caregiver", 1)
    db.commit()

    _save("251-a-chats-list.json", client.get(URL, headers=headers),
          note="Four conversations, newest activity first: one the caregiver spoke last in (from_me true), one with a long "
               "last message (cut to 140 characters), one with no message yet (last_message null), one finished visit. The "
               "booking the caregiver was only offered and another caregiver's booking are not listed.")

    p1 = {"limit": 2}
    r1 = client.get(URL, headers=headers, params=p1)
    _save("252-a-chats-page-1.json", r1, p1, note="limit=2: next_before and next_before_id say there is more.")
    body = r1.json()
    p2 = {"limit": 2, "before": body["next_before"], "before_id": body["next_before_id"]}
    _save("253-a-chats-page-2.json", client.get(URL, headers=headers, params=p2), p2,
          note="The previous page's cursor passed back: the last page has next_before and next_before_id null.")

    _save("254-a-chats-limit-too-big.json", client.get(URL, headers=headers, params={"limit": 51}), {"limit": 51})
    _save("255-p-chats-patient-token.json", client.get(URL, headers=auth_headers(ada.user)), auth=True)
    _save("256-none-chats-no-token.json", client.get(URL), auth=False)
