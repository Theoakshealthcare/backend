"""POST /booking/booking/{id}/verify-payment: same rules as the webhook."""

import hashlib
import hmac
import json

import pytest

from app.core.config import settings
from app.db.models.booking import Booking, PaymentStatus
from app.services import dispatch
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_booking, make_patient


def _url(b):
    return f"/api/v1/booking/booking/{b.id}/verify-payment"


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: calls.append(booking_id))
    return calls


def _paystack(monkeypatch, amount_kobo=4_400_000, status="success"):
    calls = []

    def fake(self, reference):
        calls.append(reference)
        return {"status": True, "data": {"id": 1, "reference": reference, "amount_kobo": amount_kobo,
                                         "status": status, "paid_at": "2026-10-05T10:00:00.000Z"}}

    monkeypatch.setattr(PaystackService, "verify_transaction", fake)
    return calls


def _setup(db, **kw):
    patient = make_patient(db)
    booking = make_booking(db, patient, payment_status=PaymentStatus.PENDING, total_amount_kobo=4_400_000, **kw)
    return patient, booking


def _fresh(db, b):
    db.expire_all()
    return db.get(Booking, b.id)


def _webhook(client, b):
    body = json.dumps({"event": "charge.success", "data": {
        "reference": b.paystack_reference, "amount": 4_400_000, "status": "success"}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/v1/payment/paystack/webhook", content=body, headers={"x-paystack-signature": sig})


def test_requires_auth(db, client):
    _, b = _setup(db)
    assert client.post(_url(b)).status_code == 401


def test_other_patient_forbidden(db, client, monkeypatch, notified):
    _, b = _setup(db)
    other = make_patient(db)
    _paystack(monkeypatch)
    assert client.post(_url(b), headers=auth_headers(other.user)).status_code == 403
    assert _fresh(db, b).payment_status == PaymentStatus.PENDING


def test_success_marks_paid_and_notifies_once(db, client, monkeypatch, notified):
    patient, b = _setup(db)
    _paystack(monkeypatch)
    r = client.post(_url(b), headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    assert r.json()["payment_status"] == "paid"
    assert _fresh(db, b).payment_status == PaymentStatus.PAID
    assert len(notified) == 1


def test_repeat_verify_does_not_renotify(db, client, monkeypatch, notified):
    patient, b = _setup(db)
    _paystack(monkeypatch)
    for _ in range(3):
        r = client.post(_url(b), headers=auth_headers(patient.user))
        assert r.status_code == 200
        assert r.json()["payment_status"] == "paid"
    assert len(notified) == 1


def test_wrong_amount_not_marked_paid(db, client, monkeypatch, notified):
    patient, b = _setup(db)
    _paystack(monkeypatch, amount_kobo=100000)
    r = client.post(_url(b), headers=auth_headers(patient.user))
    assert r.status_code == 409
    fresh = _fresh(db, b)
    assert fresh.payment_status == PaymentStatus.PENDING
    assert "mismatch" in (fresh.payment_failure_reason or "").lower()
    assert notified == []


def test_not_yet_paid_stays_pending(db, client, monkeypatch, notified):
    patient, b = _setup(db)
    _paystack(monkeypatch, status="abandoned")
    r = client.post(_url(b), headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert _fresh(db, b).payment_status == PaymentStatus.PENDING
    assert notified == []


def test_webhook_then_verify_notifies_once(db, client, monkeypatch, notified):
    patient, b = _setup(db)
    _paystack(monkeypatch)
    assert _webhook(client, b).status_code == 200
    assert client.post(_url(b), headers=auth_headers(patient.user)).status_code == 200
    assert len(notified) == 1


def test_verify_then_webhook_notifies_once(db, client, monkeypatch, notified):
    patient, b = _setup(db)
    _paystack(monkeypatch)
    assert client.post(_url(b), headers=auth_headers(patient.user)).status_code == 200
    assert _webhook(client, b).status_code == 200
    assert len(notified) == 1


def _expire(db, b):
    from datetime import datetime, timedelta

    from app.services.booking_expiry import expire_unpaid_bookings

    b.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    assert expire_unpaid_bookings(db) == 1


def test_late_verify_with_lead_time_reinstates_with_the_normal_response(db, client, monkeypatch, notified):
    _paystack(monkeypatch)
    patient, b = _setup(db)
    _expire(db, b)
    r = client.post(_url(b), headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    assert r.json()["payment_status"] == "paid" and "refund_required" not in r.json()
    assert _webhook(client, b).status_code == 200
    r = client.post(_url(b), headers=auth_headers(patient.user))
    assert "refund_required" not in r.json()
    fresh = _fresh(db, b)
    assert fresh.status.value == "pending" and fresh.dispatch_state == "dispatching"
    assert notified == [b.id]


def test_late_verify_too_close_to_the_visit_returns_the_refund_response(db, client, monkeypatch, notified):
    from datetime import datetime, timedelta

    _paystack(monkeypatch)
    soon = datetime.utcnow() + timedelta(minutes=10)
    patient, b = _setup(db, scheduled_date=soon, scheduled_end_date=soon + timedelta(hours=4))
    _expire(db, b)
    expected_keys = {"status", "message", "booking_id", "payment_status", "booking_status", "refund_required"}
    for _ in range(2):  # the repeat call (already paid) answers the same way
        r = client.post(_url(b), headers=auth_headers(patient.user))
        assert r.status_code == 200, r.text
        body = r.json()
        assert expected_keys <= set(body)
        assert body["payment_status"] == "paid" and body["booking_status"] == "cancelled"
        assert body["refund_required"] is True and body["message"]
    assert _webhook(client, b).status_code == 200
    fresh = _fresh(db, b)
    assert fresh.refund_required is True and fresh.dispatch_state is None
    assert notified == []
