"""Writes the live captures behind docs/fe-integration-guide-chat-push-payment-return.md.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_CHAT_PUSH_PAYMENT=1 poetry run pytest app/tests/test_capture_chat_push_payment.py -q

Expo is the fake transport (a push never leaves the machine): the push captures are the exact request body the app sends to
Expo, as recorded by the fake. Paystack is not involved: the callback page is captured as served.
"""

import json
import os
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.push_token import PushToken
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CHAT_PUSH_PAYMENT"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "chat-push-payment-return"
CALLBACK = "/api/v1/payment/callback"
INTERESTING_HEADERS = ("content-type", "cache-control", "content-security-policy", "referrer-policy", "x-content-type-options")


def _write(name, doc):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _http(r, method="GET", params=None, body=None, auth=True, text=False):
    return {
        "request": {"method": method, "path": r.request.url.path, "query": params or None,
                    "auth": "Bearer <ACCESS_TOKEN_JWT>" if auth else None, "body": body},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in INTERESTING_HEADERS},
                     "body": r.text if text else r.json()},
    }


def test_capture(db, client, expo):
    caregiver = make_caregiver(db, full_name="Placeholder Chidi Nwosu")
    caregiver.user.profile_image_url = "https://res.cloudinary.com/placeholder/image/upload/v1/caregivers/placeholder-chidi.jpg"
    patient = make_patient(db, full_name="Placeholder Ada Okafor")
    patient.user.profile_image_url = "https://res.cloudinary.com/placeholder/image/upload/v1/patients/placeholder-ada.jpg"
    db.add(PushToken(user_id=patient.user_id, token="ExponentPushToken[placeholder-patient-device]", platform="android"))
    db.add(PushToken(user_id=caregiver.user_id, token="ExponentPushToken[placeholder-caregiver-device]", platform="ios"))
    db.commit()
    booking = make_booking(db, patient, caregiver, status=BookingStatus.ACCEPTED)
    send = f"/api/v1/booking/chat/{booking.id}/send"

    # 1. The caregiver writes: the patient is pushed.
    body = {"message": "Placeholder: I'm on my way, about ten minutes."}
    r = client.post(send, headers=auth_headers(caregiver.user), json=body)
    assert r.status_code == 200, r.text
    doc = _http(r, "POST", body=body)
    doc["push_sent_to_expo"] = expo.batches[-1]
    doc["note"] = "The caregiver sends; the patient's device is the only one pushed (push_sent_to_expo is the request body the " \
                  "app made to Expo, recorded by the fake Expo)."
    _write("001-c-chat-send-pushes-patient.json", doc)

    # 2. The patient writes a long message: the caregiver is pushed, with a cut body and the patient's first name + initial.
    n_before = len(expo.batches)
    body = {"message": "Placeholder: " + "please bring the blue folder from the hall table and ring the second bell " * 3}
    r = client.post(send, headers=auth_headers(patient.user), json=body)
    assert r.status_code == 200, r.text
    assert len(expo.batches) == n_before + 1
    doc = _http(r, "POST", body=body)
    doc["push_sent_to_expo"] = expo.batches[-1]
    doc["note"] = "The patient sends a long message: the push body is cut to 100 characters, the title is first name and last " \
                  "initial, and only the caregiver's device is pushed."
    _write("002-p-chat-send-pushes-caregiver.json", doc)

    # 3. Chat lists carry the photos: the caregiver's list and the patient's booking payloads.
    r = client.get("/api/v1/caregiver/chats", headers=auth_headers(caregiver.user))
    assert r.status_code == 200
    _write("003-c-chats-list-patient-photo.json", _http(r))
    r = client.get(f"/api/v1/booking/booking/{booking.id}", headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    _write("004-p-booking-detail-caregiver-photo.json", _http(r))
    r = client.get("/api/v1/booking/patient/bookings", headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    _write("005-p-booking-list-caregiver-photo.json", _http(r))

    # 4. The payment return page.
    params = {"trxref": "PLACEHOLDER-REF-123", "reference": "PLACEHOLDER-REF-123"}
    r = client.get(CALLBACK, params=params)
    assert r.status_code == 200 and "PLACEHOLDER-REF-123" not in r.text
    doc = _http(r, params=params, auth=False, text=True)
    doc["note"] = "What Paystack's redirect lands on. The query is ignored: the reference is not in the page."
    _write("006-none-payment-callback.json", doc)
    bare = client.get(CALLBACK)
    xss = client.get(CALLBACK, params={"reference": '"><script>alert(1)</script>'})
    _write("007-none-payment-callback-other-queries.json", {
        "observed": {
            "bare_status": bare.status_code,
            "hostile_query_status": xss.status_code,
            "hostile_query_page_identical_to_bare_page": xss.text == bare.text,
            "hostile_value_appears_in_page": "alert(1)" in xss.text,
        },
        "note": "Computed by the capture run from two real responses.",
    })
