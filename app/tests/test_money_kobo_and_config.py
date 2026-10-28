"""Shared kobo conversion, Paystack call timeouts, and webhook key hardening."""

import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from app.core.config import Settings, settings
from app.db.models.booking import Booking, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.schemas.booking import BookingCreate
from app.services import dispatch
from app.services.booking import BookingService
from app.services.booking_payment import expected_kobo
from app.services.paystack import PaystackService
from app.tests.factories import make_booking, make_patient
from app.tests.fake_prembly import PRODUCTION_KYC_KEYS


def test_to_kobo_rounds_half_up():
    from app.core.money import to_kobo

    assert to_kobo(1531.875) == 153188
    assert to_kobo(1531.885) == 153189  # half-even would give 153188
    assert to_kobo(Decimal("1531.874")) == 153187
    assert to_kobo(44000.0) == 4_400_000
    assert to_kobo(0.1 + 0.2) == 30  # float noise must not shift the result


def test_initialize_amount_equals_settlement_expectation(db, monkeypatch):
    """The amount we send to Paystack must be exactly what settlement later expects."""
    sent = {}

    class Resp:
        def raise_for_status(self): ...
        def json(self):
            return {"status": True, "data": {"authorization_url": "u", "access_code": "a", "reference": "r"}}

    def fake_post(url, **kwargs):
        sent.update(kwargs)
        return Resp()

    monkeypatch.setattr("app.services.paystack.requests.post", fake_post)
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, total_amount_kobo=153189)
    PaystackService().initialize_transaction("p@example.com", booking.total_amount_kobo, "ref-1")
    assert sent["json"]["amount"] == expected_kobo(booking) == 153189


def test_fractional_payment_settles_end_to_end(db, client, monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, total_amount_kobo=153189)
    body = json.dumps({"event": "charge.success", "data": {
        "reference": booking.paystack_reference, "amount": 153189, "status": "success"}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    assert client.post("/api/v1/payment/paystack/webhook", content=body,
                       headers={"x-paystack-signature": sig}).status_code == 200
    db.expire_all()
    assert db.get(Booking, booking.id).payment_status == PaymentStatus.PAID


def test_booking_creation_stores_integer_kobo(db, monkeypatch):
    sent = {}

    def fake_init(self, email, amount_kobo, reference, callback_url=None, metadata=None):
        sent["amount_kobo"] = amount_kobo
        return {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference}

    monkeypatch.setattr(PaystackService, "initialize_transaction", fake_init)
    db.add(BookingSettings(base_fee_kobo=123457, platform_fee_percent=10.0, multipliers={}))
    db.commit()
    patient = make_patient(db)
    result = BookingService(db).create_booking(patient.id, BookingCreate(
        service_type="nurse", assistance_type="x", duration_hours=1,
        scheduled_date="2030-01-01T10:00:00", patient_address="1 Test Close",
        patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
        patient_state="Lagos", booking_for="myself"))
    b = result["booking"]
    for v in (b.total_amount_kobo, b.service_fee_kobo, b.platform_fee_kobo):
        assert isinstance(v, int)
    assert sent["amount_kobo"] == b.total_amount_kobo == expected_kobo(b)


def test_every_paystack_request_has_a_timeout(monkeypatch):
    seen = []

    class Resp:
        def raise_for_status(self): ...
        def json(self):
            return {"status": True, "data": {"id": 1, "reference": "r", "amount": 100, "status": "success",
                                             "authorization_url": "u", "access_code": "a"}}

    def fake(url, **kwargs):
        seen.append(kwargs.get("timeout"))
        return Resp()

    monkeypatch.setattr("app.services.paystack.requests.post", fake)
    monkeypatch.setattr("app.services.paystack.requests.get", fake)
    svc = PaystackService()
    svc.initialize_transaction("p@example.com", 1000, "r")
    svc.verify_transaction("r")
    svc.initiate_refund("r", 1000)
    svc.get_supported_banks()
    assert len(seen) == 4 and all(t for t in seen)


# --- webhook key hardening -----------------------------------------------------

def test_webhook_rejected_when_secret_key_empty(db, client, monkeypatch):
    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", "")
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    body = json.dumps({"event": "charge.success", "data": {
        "reference": booking.paystack_reference, "amount": 4_400_000, "status": "success"}}).encode()
    forged = hmac.new(b"", body, hashlib.sha512).hexdigest()  # anyone can compute this
    r = client.post("/api/v1/payment/paystack/webhook", content=body, headers={"x-paystack-signature": forged})
    assert r.status_code in (400, 401)
    db.expire_all()
    assert db.get(Booking, booking.id).payment_status == PaymentStatus.PENDING


def test_production_startup_requires_paystack_key():
    with pytest.raises(ValueError):
        Settings(ENVIRONMENT="production", PAYSTACK_SECRET_KEY="", **PRODUCTION_KYC_KEYS)
    assert Settings(ENVIRONMENT="production", PAYSTACK_SECRET_KEY="sk_live_placeholder", **PRODUCTION_KYC_KEYS).ENVIRONMENT == "production"
    assert Settings(ENVIRONMENT="development", PAYSTACK_SECRET_KEY="").PAYSTACK_SECRET_KEY == ""


def test_webhook_db_work_runs_off_the_event_loop(db, client, monkeypatch):
    import asyncio

    import app.api.v1.endpoints.payment as pay

    on_loop = []
    real = pay.settle_booking_payment

    def spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(*a, **kw)

    monkeypatch.setattr(pay, "settle_booking_payment", spy)
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    body = json.dumps({"event": "charge.success", "data": {
        "reference": booking.paystack_reference, "amount": 4_400_000, "status": "success"}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    assert client.post("/api/v1/payment/paystack/webhook", content=body,
                       headers={"x-paystack-signature": sig}).status_code == 200
    assert on_loop == [False]


def test_production_startup_pins_the_paystack_base_url():
    # The secret key is sent as a Bearer token to this URL and its "success" is trusted: it must be Paystack.
    good = dict(ENVIRONMENT="production", PAYSTACK_SECRET_KEY="sk_live_placeholder", **PRODUCTION_KYC_KEYS)
    assert Settings(**good).PAYSTACK_BASE_URL == "https://api.paystack.co"
    assert Settings(**good, PAYSTACK_BASE_URL="https://api.paystack.co/").ENVIRONMENT == "production"
    for bad in ("http://evil.example", "https://api.paystack.co.evil.example", "http://localhost:9999"):
        with pytest.raises(ValueError, match="PAYSTACK_BASE_URL"):
            Settings(**good, PAYSTACK_BASE_URL=bad)
    # development may point at a fake (the local end-to-end script does)
    assert Settings(ENVIRONMENT="development", PAYSTACK_BASE_URL="http://fake:9999").PAYSTACK_BASE_URL == "http://fake:9999"
