"""Writes the live captures behind docs/fe-integration-guide-patient-booking-reschedule.md.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_RESCHEDULE=1 poetry run pytest app/tests/test_capture_booking_reschedule.py -q

Expo is the fake transport (a push never leaves the machine). Paystack is not involved: the bookings are created already paid
(as after a settled payment); the payment itself is documented in the patient-bookings guide. Data is placeholder.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db.models.booking import BookingNotification, BookingStatus, PaymentStatus
from app.db.models.push_token import PushToken
from app.tests.booking_fixtures import make_prices
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.policy_helpers import set_policy
from app.tests.test_dispatch import make_candidate

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_RESCHEDULE"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "booking-reschedule"
API = "/api/v1/booking"


def _write(name, doc):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _http(r, method="GET", body=None, query=None):
    return {
        "request": {"method": method, "path": r.request.url.path, "query": query, "auth": "Bearer <ACCESS_TOKEN_JWT>",
                    "body": body},
        "response": {"status_code": r.status_code, "body": r.json() if r.content else None},
    }


def _iso(dt):
    return dt.isoformat() + "Z"


def _booking(db, patient, start_in, caregiver=None, **kw):
    start = datetime.utcnow() + timedelta(hours=start_in)
    status = BookingStatus.ACCEPTED if caregiver else BookingStatus.PENDING
    return make_booking(db, patient, caregiver, status=status, scheduled_date=start,
                        scheduled_end_date=start + timedelta(hours=4), visit_code="4821" if caregiver else None, **kw)


def test_capture(db, client, expo):
    set_policy(db)
    make_prices(db)
    patient = make_patient(db, full_name="Placeholder Ada Okafor")
    p = auth_headers(patient.user)
    now = datetime.utcnow()
    cg = make_candidate(db, at=now, full_name="Placeholder Chidi Nwosu")
    db.add(PushToken(user_id=cg.user_id, token="ExponentPushToken[placeholder-caregiver]", platform="ios"))
    db.commit()

    # a booking still looking for a caregiver, then an accepted one
    open_booking = _booking(db, patient, 40, dispatch_state="unmatched", dispatch_started_at=now - timedelta(hours=1))
    accepted = _booking(db, patient, 30, caregiver=cg, dispatch_state="matched")
    db.add(BookingNotification(booking_id=accepted.id, caregiver_id=cg.id, title="New Booking Request", message="m",
                               notification_type="new_booking", response="accept"))
    db.commit()
    new1, new2 = now + timedelta(hours=72), now + timedelta(hours=96)

    r = client.get(f"{API}/{open_booking.id}/reschedule-terms", headers=p)
    _write("001-p-terms.json", _http(r))
    q = {"new_scheduled_date": _iso(new1)}
    r = client.get(f"{API}/{open_booking.id}/reschedule-terms", params=q, headers=p)
    _write("002-p-terms-with-target.json", _http(r, query=q))
    q = {"new_scheduled_date": _iso(now + timedelta(days=29))}
    r = client.get(f"{API}/{open_booking.id}/reschedule-terms", params=q, headers=p)
    _write("003-p-terms-target-too-far.json", _http(r, query=q))

    body = {"new_scheduled_date": _iso(new1), "request_id": "11111111-1111-4111-8111-111111111111",
            "accepted_total_kobo": 4400000}
    r = client.post(f"{API}/{open_booking.id}/reschedule", json=body, headers=p)
    _write("004-p-reschedule-unassigned.json", _http(r, "POST", body))
    r = client.post(f"{API}/{open_booking.id}/reschedule", json=body, headers=p)
    _write("005-p-reschedule-replay.json", _http(r, "POST", body))

    expo.messages.clear()
    body = {"new_scheduled_date": _iso(new2), "request_id": "22222222-2222-4222-8222-222222222222"}
    r = client.post(f"{API}/{accepted.id}/reschedule", json=body, headers=p)
    _write("006-p-reschedule-caregiver-released.json", _http(r, "POST", body))
    _write("007-expo-push-to-released-caregiver.json", {
        "note": "The request the app made to Expo for the released caregiver (the job-offer push for the new wave is the other message).",
        "observed": [m for m in expo.messages if m["data"].get("type") == "booking_rescheduled"]})
    _write("008-expo-push-job-offer-new-time.json", {
        "note": "The re-offer to caregivers for the new time (existing job_offer push; scheduled_at is the NEW time).",
        "observed": [m for m in expo.messages if m["data"].get("type") == "job_offer"]})

    body = {"new_scheduled_date": _iso(now + timedelta(hours=120))}
    r = client.post(f"{API}/{accepted.id}/reschedule", json=body, headers=p)
    _write("009-p-reschedule-second-free-one.json", _http(r, "POST", body))
    body = {"new_scheduled_date": _iso(now + timedelta(hours=140))}
    r = client.post(f"{API}/{accepted.id}/reschedule", json=body, headers=p)
    _write("010-p-reschedule-limit-reached.json", _http(r, "POST", body))

    near = _booking(db, patient, 6)
    r = client.get(f"{API}/{near.id}/reschedule-terms", headers=p)
    _write("011-p-terms-window-closed.json", _http(r))
    body = {"new_scheduled_date": _iso(new1)}
    r = client.post(f"{API}/{near.id}/reschedule", json=body, headers=p)
    _write("012-p-reschedule-window-closed.json", _http(r, "POST", body))

    far = _booking(db, patient, 50)
    body = {"new_scheduled_date": _iso(now + timedelta(days=29))}
    r = client.post(f"{API}/{far.id}/reschedule", json=body, headers=p)
    _write("013-p-reschedule-too-far.json", _http(r, "POST", body))
    body = {"new_scheduled_date": _iso(far.scheduled_date)}
    r = client.post(f"{API}/{far.id}/reschedule", json=body, headers=p)
    _write("014-p-reschedule-same-time.json", _http(r, "POST", body))
    body = {"new_scheduled_date": _iso(now + timedelta(hours=6))}
    r = client.post(f"{API}/{far.id}/reschedule", json=body, headers=p)
    _write("015-p-reschedule-too-soon.json", _http(r, "POST", body))

    cheaper = _booking(db, patient, 50, total_amount_kobo=5_000_000)       # paid 50,000; the price now is 44,000
    r = client.get(f"{API}/{cheaper.id}/reschedule-terms", headers=p)
    _write("016-p-terms-lower-price.json", _http(r))
    body = {"new_scheduled_date": _iso(new1), "accepted_total_kobo": 5000000}
    r = client.post(f"{API}/{cheaper.id}/reschedule", json=body, headers=p)
    _write("017-p-reschedule-price-changed.json", _http(r, "POST", body))
    body = {"new_scheduled_date": _iso(new1), "accepted_total_kobo": 4400000}
    r = client.post(f"{API}/{cheaper.id}/reschedule", json=body, headers=p)
    _write("018-p-reschedule-lower-price-credit.json", _http(r, "POST", body))

    dearer = _booking(db, patient, 50, total_amount_kobo=3_300_000)        # paid 33,000; the price now is 44,000
    r = client.get(f"{API}/{dearer.id}/reschedule-terms", headers=p)
    _write("019-p-terms-higher-price.json", _http(r))
    body = {"new_scheduled_date": _iso(new1)}
    r = client.post(f"{API}/{dearer.id}/reschedule", json=body, headers=p)
    _write("020-p-reschedule-price-increase-refused.json", _http(r, "POST", body))

    unpaid = _booking(db, patient, 50, payment_status=PaymentStatus.PENDING)
    r = client.post(f"{API}/{unpaid.id}/reschedule", json=body, headers=p)
    _write("021-p-reschedule-unpaid.json", _http(r, "POST", body))
    stranger = auth_headers(make_patient(db).user)
    r = client.post(f"{API}/{far.id}/reschedule", json=body, headers=stranger)
    _write("022-p-reschedule-not-your-booking.json", _http(r, "POST", body))

    r = client.get(f"{API}/patient/bookings", headers=p)
    keys = ("id", "status", "scheduled_date", "reschedule_count", "can_reschedule", "reschedules_left", "dispatch_state")
    _write("023-p-bookings-list-fields.json", {
        "request": _http(r)["request"], "response": {"status_code": r.status_code, "body": [
            {k: b[k] for k in keys} for b in sorted(r.json(), key=lambda b: b["id"])]}})

    a = auth_headers(make_admin(db, ["super_admin"], password=None))
    r = client.get(f"/api/v1/admin/bookings/{cheaper.id}", headers=a)
    detail = r.json()
    _write("024-a-booking-detail-reschedules.json", {
        "request": {**_http(r)["request"], "auth": "Bearer <ADMIN_ACCESS_TOKEN_JWT>"},
        "response": {"status_code": r.status_code, "body": {k: detail[k] for k in ("scheduled_date", "reschedule_count", "reschedules")}}})
    body = {"reason": "Pilot settings", "reschedule_free_hours": 12, "reschedule_max_free": 2, "reschedule_max_days_ahead": 28}
    r = client.patch("/api/v1/admin/booking-policy", json=body, headers=a)
    _write("025-a-policy-patch.json", {**_http(r, "PATCH", body), "response": {
        "status_code": r.status_code, "body": {k: v for k, v in r.json().items() if k.startswith("reschedule")}}})
