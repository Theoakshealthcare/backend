"""E2: money is stored as integer kobo; responses stay in naira."""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal

import pytest

from app.core.config import settings
from app.core.money import kobo_to_naira, to_kobo
from app.db.models.booking import Booking, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.db.models.patient import PatientRefundRequest, PatientTransaction, PatientWallet
from app.db.models.prescription_settings import PrescriptionSettings
from app.schemas.booking import BookingCreate, BookingPreviewRequest
from app.services.booking import BookingService
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_admin, make_patient

WEBHOOK = "/api/v1/payment/paystack/webhook"


# --- kobo_to_naira ---------------------------------------------------------------

def test_kobo_to_naira_is_exact_two_places():
    assert kobo_to_naira(0) == Decimal("0.00")
    assert kobo_to_naira(1) == Decimal("0.01")
    assert kobo_to_naira(153189) == Decimal("1531.89")
    assert kobo_to_naira(4_400_000) == Decimal("44000.00")
    assert kobo_to_naira(-250) == Decimal("-2.50")
    assert str(kobo_to_naira(10)) == "0.10"


def test_kobo_to_naira_round_trips_with_to_kobo():
    for k in (0, 1, 99, 100, 123457, 999_999_999_999):
        assert to_kobo(kobo_to_naira(k)) == k


# --- schema: columns are BIGINT kobo ---------------------------------------------

@pytest.mark.parametrize("table,column", [
    ("bookings", "total_amount_kobo"), ("bookings", "service_fee_kobo"),
    ("bookings", "platform_fee_kobo"), ("booking_settings", "base_fee_kobo"),
    ("patient_wallets", "balance_kobo"), ("patient_wallets", "total_deposited_kobo"),
    ("patient_wallets", "total_spent_kobo"), ("patient_wallets", "total_refunded_kobo"),
    ("patient_transactions", "amount_kobo"), ("patient_refund_requests", "amount_kobo"),
    ("prescriptions", "total_cost_kobo"), ("prescription_settings", "base_fee_kobo"),
])
def test_money_columns_are_bigint_kobo(db, table, column):
    from sqlalchemy import text

    row = db.execute(text(
        "SELECT data_type FROM information_schema.columns WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column}).first()
    assert row is not None and row[0] == "bigint"


# --- fractional price: preview -> create -> Paystack init -> settlement agree -------

@pytest.fixture
def fractional_pricing(db):
    # 1234.57/h, x1.3 multiplier, 3.3% discount, 7.5% platform: every step lands on a fraction of a naira
    db.add(BookingSettings(base_fee_kobo=123457, platform_fee_percent=7.5,
                           multipliers={"nurse": 1.3}, discount=3.3))
    db.commit()


def _create_body():
    return dict(service_type="nurse", assistance_type="x", duration_hours=3,
                scheduled_date="2030-01-01T10:00:00", patient_address="1 Test Close",
                patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
                patient_state="Lagos", booking_for="myself")


def test_preview_create_init_and_settlement_agree_on_one_kobo_total(db, client, monkeypatch, fractional_pricing):
    sent = {}

    def fake_init(self, email, amount_kobo, reference, callback_url=None, metadata=None):
        sent["amount_kobo"] = amount_kobo
        return {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference}

    monkeypatch.setattr(PaystackService, "initialize_transaction", fake_init)
    monkeypatch.setattr(BookingService, "notify_nearby_caregivers", lambda self, b: [])
    patient = make_patient(db)

    preview = BookingService(db).calculate_booking_preview(BookingPreviewRequest(
        service_type="nurse", duration_hours=3))
    result = BookingService(db).create_booking(patient.id, BookingCreate(**_create_body()))
    b = result["booking"]

    assert isinstance(b.total_amount_kobo, int)
    assert b.total_amount_kobo == b.service_fee_kobo + b.platform_fee_kobo
    assert sent["amount_kobo"] == b.total_amount_kobo
    assert to_kobo(preview.total_amount) == b.total_amount_kobo
    assert to_kobo(preview.service_fee) == b.service_fee_kobo
    assert to_kobo(preview.platform_fee) == b.platform_fee_kobo
    assert b.total_amount_kobo % 100 != 0  # the case is genuinely fractional

    body = json.dumps({"event": "charge.success", "data": {
        "reference": b.paystack_reference, "amount": b.total_amount_kobo, "status": "success"}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    assert client.post(WEBHOOK, content=body, headers={"x-paystack-signature": sig}).status_code == 200
    db.expire_all()
    assert db.get(Booking, b.id).payment_status == PaymentStatus.PAID


def test_booking_response_still_naira_two_places(db, client, monkeypatch, fractional_pricing):
    monkeypatch.setattr(PaystackService, "initialize_transaction",
                        lambda self, email, amount_kobo, reference, callback_url=None, metadata=None:
                        {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference})
    patient = make_patient(db)
    r = client.post("/api/v1/booking/create", json=_create_body(), headers=auth_headers(patient.user))
    assert r.status_code in (200, 201), r.text
    data = r.json()
    b = db.query(Booking).one()
    assert to_kobo(data["total_amount"]) == b.total_amount_kobo
    assert data["total_amount"] == float(kobo_to_naira(b.total_amount_kobo))
    assert data["service_fee"] == float(kobo_to_naira(b.service_fee_kobo))
    assert data["platform_fee"] == float(kobo_to_naira(b.platform_fee_kobo))


def test_booking_settings_api_keeps_naira_base_fee(db, client, fractional_pricing):
    r = client.get("/api/v1/booking-settings/latest")
    assert r.status_code == 200
    assert r.json()["base_fee"] == 1234.57
    admin = make_admin(db)
    r = client.post("/api/v1/booking-settings/", headers=auth_headers(admin), json={
        "base_fee": 2500.5, "platform_fee_percent": 10.0})
    assert r.status_code == 200, r.text
    assert r.json()["base_fee"] == 2500.5
    db.expire_all()
    assert db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).first().base_fee_kobo == 250050


def test_prescription_priced_in_kobo_and_returned_in_naira(db, client):
    from app.db.models.prescription import Prescription

    db.add(PrescriptionSettings(base_fee_kobo=333333, multipliers={"regular": 1.1}, discount=2.5))
    db.commit()
    patient = make_patient(db)
    r = client.post("/api/v1/prescription/create", headers=auth_headers(patient.user), json={
        "patient_id": str(patient.id), "requested_by_user_id": str(patient.user_id),
        "medication_name": "X", "delivery_option": "regular", "address": "1 Test Close"})
    assert r.status_code == 200, r.text
    rx = db.query(Prescription).one()
    assert isinstance(rx.total_cost_kobo, int)
    # 3333.33 * 1.1 * 0.975 = 3575.0 to the kobo, rounded half-up
    assert rx.total_cost_kobo == to_kobo(Decimal("3333.33") * Decimal("1.1") * Decimal("0.975"))
    assert r.json()["total_cost"] == float(kobo_to_naira(rx.total_cost_kobo))


# --- wallet: deposit -> payment -> refund, all kobo -------------------------------

def _hook(client, reference, kobo):
    body = json.dumps({"event": "charge.success", "data": {
        "reference": reference, "amount": kobo, "status": "success", "channel": "card"}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post(WEBHOOK, content=body, headers={"x-paystack-signature": sig})


def test_wallet_deposit_payment_refund_in_kobo(db, client, monkeypatch):
    sent = {}

    def fake_init(self, email, amount_kobo, reference, callback_url=None, metadata=None):
        sent["amount_kobo"] = amount_kobo
        return {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference}

    monkeypatch.setattr(PaystackService, "initialize_transaction", fake_init)
    patient = make_patient(db)
    h = auth_headers(patient.user)

    r = client.post("/api/v1/patient/wallet/deposit", json={"amount": "1234.56"}, headers=h)
    assert r.status_code == 200, r.text
    assert sent["amount_kobo"] == 123456
    assert Decimal(str(r.json()["amount"])) == Decimal("1234.56")
    tx = db.query(PatientTransaction).one()
    assert tx.amount_kobo == 123456

    assert _hook(client, tx.paystack_reference, 123456).status_code == 200
    w = client.get("/api/v1/patient/wallet", headers=h).json()
    assert Decimal(str(w["balance"])) == Decimal("1234.56")
    assert Decimal(str(w["total_deposited"])) == Decimal("1234.56")
    db.expire_all()
    wallet = db.query(PatientWallet).one()
    assert (wallet.balance_kobo, wallet.total_deposited_kobo) == (123456, 123456)

    r = client.post("/api/v1/patient/wallet/payment", json={"amount": "500.25"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "successful"
    assert Decimal(str(r.json()["remaining_balance"])) == Decimal("734.31")
    db.expire_all()
    wallet = db.query(PatientWallet).one()
    assert (wallet.balance_kobo, wallet.total_spent_kobo) == (73431, 50025)

    pay = db.query(PatientTransaction).filter_by(type="payment").one()
    assert pay.amount_kobo == 50025
    r = client.post("/api/v1/patient/wallet/refund/request", headers=h, json={
        "transaction_id": str(pay.id), "amount": "100.10", "reason": "Service was not delivered"})
    assert r.status_code == 200, r.text
    req = db.query(PatientRefundRequest).one()
    assert req.amount_kobo == 10010

    admin = make_admin(db)
    r = client.put(f"/api/v1/patient/admin/refund/requests/{req.id}",
                   json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    db.expire_all()
    wallet = db.query(PatientWallet).one()
    assert (wallet.balance_kobo, wallet.total_refunded_kobo) == (83441, 10010)

    w = client.get("/api/v1/patient/wallet", headers=h).json()
    assert Decimal(str(w["balance"])) == Decimal("834.41")
    assert Decimal(str(w["total_refunded"])) == Decimal("100.10")

    txs = client.get("/api/v1/patient/wallet/transactions", headers=h).json()
    assert sorted(Decimal(str(t["amount"])) for t in txs) == [
        Decimal("100.10"), Decimal("500.25"), Decimal("1234.56")]
    reqs = client.get("/api/v1/patient/wallet/refund/requests", headers=h).json()
    assert Decimal(str(reqs[0]["amount"])) == Decimal("100.10")


def test_paystack_refund_called_with_kobo(db, client, monkeypatch):
    calls = []
    monkeypatch.setattr(PaystackService, "initiate_refund",
                        lambda self, ref, amount_kobo=None: calls.append((ref, amount_kobo)) or
                        {"status": True, "data": {"id": 1}})
    patient = make_patient(db)
    wallet = PatientWallet(patient_id=patient.id, balance_kobo=0)
    db.add(wallet)
    db.commit()
    tx = PatientTransaction(id=uuid.uuid4(), patient_id=patient.id, wallet_id=wallet.id, type="payment",
                            amount_kobo=123457, status="successful", paystack_reference="PAY_1")
    db.add(tx)
    db.commit()
    req = PatientRefundRequest(id=uuid.uuid4(), patient_id=patient.id, transaction_id=tx.id,
                               amount_kobo=100001, reason="Service was not delivered", status="pending")
    db.add(req)
    db.commit()
    r = client.put(f"/api/v1/patient/admin/refund/requests/{req.id}", json={"action": "approve"},
                   headers=auth_headers(make_admin(db)))
    assert r.status_code == 200, r.text
    assert calls == [("PAY_1", 100001)]


def test_deposit_wrong_kobo_not_credited_and_exact_kobo_credited(db, client):
    patient = make_patient(db)
    wallet = PatientWallet(patient_id=patient.id)
    db.add(wallet)
    db.commit()
    tx = PatientTransaction(id=uuid.uuid4(), patient_id=patient.id, wallet_id=wallet.id, type="deposit",
                            amount_kobo=123457, status="pending", paystack_reference="DEP_x1")
    db.add(tx)
    db.commit()
    _hook(client, "DEP_x1", 123456)  # one kobo short
    db.expire_all()
    assert db.query(PatientWallet).one().balance_kobo == 0
    _hook(client, "DEP_x1", 123457)
    db.expire_all()
    assert db.query(PatientWallet).one().balance_kobo == 123457


def test_every_kobo_column_is_bigint():
    from sqlalchemy import BigInteger

    import app.db.models  # noqa: F401
    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.name.endswith("_kobo"):
                assert isinstance(col.type, BigInteger), f"{table.name}.{col.name}"


# --- pricing arithmetic -----------------------------------------------------------

def test_compute_price_total_is_exactly_service_plus_platform():
    from app.services.pricing import compute_price

    for hours in range(1, 25):
        p = compute_price(base_fee_kobo=123457, quantity=hours, multiplier=1.3, surge_multiplier=1.15,
                          discount_percent=3.3, platform_fee_percent=7.5)
        assert p.total_kobo == p.service_fee_kobo + p.platform_fee_kobo
        assert all(isinstance(v, int) for v in vars(p).values())


def test_compute_price_rounds_half_up_and_ignores_inactive_surge_and_discount():
    from app.services.pricing import compute_price

    # 0.5 kobo of platform fee rounds up, not to even
    p = compute_price(base_fee_kobo=5, platform_fee_percent=10)
    assert (p.service_fee_kobo, p.platform_fee_kobo, p.total_kobo) == (5, 1, 6)
    plain = compute_price(base_fee_kobo=500000, quantity=4, platform_fee_percent=10)
    assert plain.total_kobo == 2_200_000
    assert compute_price(base_fee_kobo=500000, quantity=4, platform_fee_percent=10,
                         surge_multiplier=None, discount_percent=0) == plain
