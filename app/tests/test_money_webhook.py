"""Paystack webhook: signature, replay safety, amount check, failed-after-paid."""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal

import pytest

from app.core.money import to_kobo
from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.patient import PatientTransaction, PatientWallet
from app.services import dispatch
from app.tests.factories import make_booking, make_patient

URL = "/api/v1/payment/paystack/webhook"


def _sign(body: bytes) -> str:
    return hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()


def _post(client, event, reference, kobo, signature=None, **extra):
    data = {"reference": reference, "amount": kobo, "status": "success", "channel": "card",
            "gateway_response": "Declined", "customer": {"email": "p@example.com"}}
    data.update(extra)
    body = json.dumps({"event": event, "data": data}).encode()
    headers = {"content-type": "application/json",
               "x-paystack-signature": _sign(body) if signature is None else signature}
    return client.post(URL, content=body, headers=headers)


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: calls.append(booking_id))
    return calls


def _booking(db, payment_status=PaymentStatus.PENDING, total_kobo=4_400_000, **kw):
    return make_booking(db, make_patient(db), payment_status=payment_status, total_amount_kobo=total_kobo, **kw)


def _fresh(db, booking):
    db.expire_all()
    return db.get(Booking, booking.id)


def test_bad_signature_rejected_and_nothing_changes(db, client, notified):
    b = _booking(db)
    r = _post(client, "charge.success", b.paystack_reference, 4_400_000, signature="deadbeef")
    assert r.status_code == 400
    assert _fresh(db, b).payment_status == PaymentStatus.PENDING
    assert notified == []


def test_missing_signature_rejected(db, client, notified):
    b = _booking(db)
    r = _post(client, "charge.success", b.paystack_reference, 4_400_000, signature="")
    assert r.status_code == 400
    assert _fresh(db, b).payment_status == PaymentStatus.PENDING


def test_signature_for_a_different_body_rejected(db, client, notified):
    b = _booking(db)
    r = _post(client, "charge.success", b.paystack_reference, 4_400_000, signature=_sign(b"{}"))
    assert r.status_code == 400


def test_valid_charge_success_marks_paid_and_notifies_once(db, client, notified):
    b = _booking(db)
    r = _post(client, "charge.success", b.paystack_reference, 4_400_000)
    assert r.status_code == 200, r.text
    fresh = _fresh(db, b)
    assert fresh.payment_status == PaymentStatus.PAID
    assert fresh.status == BookingStatus.PENDING
    assert len(notified) == 1


def test_replayed_charge_success_does_not_renotify(db, client, notified):
    b = _booking(db)
    for _ in range(3):
        assert _post(client, "charge.success", b.paystack_reference, 4_400_000).status_code == 200
    assert _fresh(db, b).payment_status == PaymentStatus.PAID
    assert len(notified) == 1


def test_wrong_amount_is_not_marked_paid_and_is_recorded(db, client, notified):
    b = _booking(db)
    r = _post(client, "charge.success", b.paystack_reference, 100_000)  # N1,000 for a N44,000 booking
    assert r.status_code == 200  # acknowledged so Paystack stops retrying
    fresh = _fresh(db, b)
    assert fresh.payment_status == PaymentStatus.PENDING
    assert "mismatch" in (fresh.payment_failure_reason or "").lower()
    assert notified == []


def test_underpay_then_correct_payment_still_settles(db, client, notified):
    b = _booking(db)
    _post(client, "charge.success", b.paystack_reference, 100_000)
    _post(client, "charge.success", b.paystack_reference, 4_400_000)
    assert _fresh(db, b).payment_status == PaymentStatus.PAID
    assert len(notified) == 1


def test_charge_failed_after_paid_does_not_cancel(db, client, notified):
    b = _booking(db, payment_status=PaymentStatus.PAID)
    r = _post(client, "charge.failed", b.paystack_reference, 4_400_000)
    assert r.status_code == 200
    fresh = _fresh(db, b)
    assert fresh.payment_status == PaymentStatus.PAID
    assert fresh.status == BookingStatus.PENDING
    assert fresh.payment_failure_reason is None


def test_charge_failed_on_unpaid_booking_still_cancels(db, client, notified):
    b = _booking(db)
    _post(client, "charge.failed", b.paystack_reference, 4_400_000)
    fresh = _fresh(db, b)
    assert fresh.payment_status == PaymentStatus.FAILED
    assert fresh.status == BookingStatus.CANCELLED


def test_cancelled_booking_that_receives_money_is_not_redispatched(db, client, notified):
    b = _booking(db, payment_status=PaymentStatus.FAILED, status=BookingStatus.CANCELLED)
    _post(client, "charge.success", b.paystack_reference, 4_400_000)
    assert notified == []


# --- wallet deposits ---------------------------------------------------------

def _deposit(db, amount="5000", status="pending"):
    patient = make_patient(db)
    wallet = PatientWallet(patient_id=patient.id, balance_kobo=0)
    db.add(wallet)
    db.commit()
    tx = PatientTransaction(
        id=uuid.uuid4(), patient_id=patient.id, wallet_id=wallet.id, type="deposit",
        amount_kobo=to_kobo(amount), status=status, paystack_reference=f"DEP_{uuid.uuid4().hex[:10]}",
    )
    db.add(tx)
    db.commit()
    return wallet, tx


def _wallet_fresh(db, wallet):
    db.expire_all()
    return db.get(PatientWallet, wallet.id)


def test_deposit_credited_once_on_replay(db, client):
    wallet, tx = _deposit(db)
    for _ in range(3):
        assert _post(client, "charge.success", tx.paystack_reference, 500_000).status_code == 200
    w = _wallet_fresh(db, wallet)
    assert w.balance_kobo == 500000 and w.total_deposited_kobo == 500000


def test_deposit_wrong_amount_not_credited(db, client):
    wallet, tx = _deposit(db)
    _post(client, "charge.success", tx.paystack_reference, 100_000)
    assert _wallet_fresh(db, wallet).balance_kobo == 0
    db.expire_all()
    assert db.get(PatientTransaction, tx.id).status == "pending"


def test_deposit_failed_after_success_does_not_flip_status(db, client):
    wallet, tx = _deposit(db)
    _post(client, "charge.success", tx.paystack_reference, 500_000)
    _post(client, "charge.failed", tx.paystack_reference, 500_000)
    db.expire_all()
    assert db.get(PatientTransaction, tx.id).status == "successful"
    assert _wallet_fresh(db, wallet).balance_kobo == 500000


def test_concurrent_replays_notify_once(db, notified):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    b = _booking(db)

    def hit():
        c = TestClient(fastapi_app, raise_server_exceptions=False)
        return _post(c, "charge.success", b.paystack_reference, 4_400_000).status_code

    with ThreadPoolExecutor(4) as ex:
        codes = [f.result(timeout=30) for f in [ex.submit(hit) for _ in range(4)]]
    assert codes == [200] * 4
    assert len(notified) == 1
