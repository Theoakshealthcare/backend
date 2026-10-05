"""Two-phase refund (no Paystack call under DB locks), refund-of-refund, refund webhooks,
and prescription payments."""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models.patient import PatientRefundRequest, PatientTransaction, PatientWallet
from app.db.models.prescription import Prescription
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_admin, make_patient
from app.tests.test_money_refunds import _fresh, _request, _tx, _url, _wallet

WEBHOOK = "/api/v1/payment/paystack/webhook"


def _event(client, event, data):
    body = json.dumps({"event": event, "data": data}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post(WEBHOOK, content=body, headers={"x-paystack-signature": sig})


def _deposit_refund_setup(db, balance="1000"):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, balance)
    tx = _tx(db, wallet, patient, "1000", reference=f"DEP_{uuid.uuid4().hex[:8]}", type_="deposit")
    req = _request(db, patient, tx, "1000")
    return admin, patient, wallet, tx, req


def _refund_txs(db):
    db.expire_all()
    return db.query(PatientTransaction).filter_by(type="refund").all()


# --- two-phase ---------------------------------------------------------------

def test_paystack_is_called_outside_any_db_transaction(db, client, monkeypatch):
    """While Paystack is being called the committed state must already show 'processing'
    and the wallet debit, and no row lock may be held."""
    admin, patient, wallet, tx, req = _deposit_refund_setup(db)
    observed = {}

    def spy(self, reference, amount=None):
        from sqlalchemy import text

        from app.db.session import SessionLocal
        other = SessionLocal()
        try:
            other.execute(text("SET LOCAL lock_timeout = '500ms'"))
            # NOWAIT would raise if the request row were still locked by the approving transaction
            row = other.execute(
                text("SELECT status FROM patient_refund_requests WHERE id = :i FOR UPDATE NOWAIT"),
                {"i": str(req.id)},
            ).one()
            bal = other.execute(
                text("SELECT balance FROM patient_wallets WHERE id = :i"), {"i": str(wallet.id)}
            ).scalar_one()
            observed.update(status=row[0], balance=bal)
        finally:
            other.rollback()
            other.close()
        return {"status": True, "data": {"id": 555}}

    monkeypatch.setattr(PaystackService, "initiate_refund", spy)
    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    assert observed == {"status": "processing", "balance": Decimal("0.00")}


def test_success_finalises_request_and_records_gateway_id(db, client, monkeypatch):
    monkeypatch.setattr(PaystackService, "initiate_refund",
                        lambda self, ref, amount=None: {"status": True, "data": {"id": 555}})
    admin, patient, wallet, tx, req = _deposit_refund_setup(db)
    assert client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin)).status_code == 200
    assert _fresh(db, PatientRefundRequest, req.id).status == "approved"
    (rt,) = _refund_txs(db)
    assert rt.paystack_reference == "555" and rt.status == "pending"
    assert _fresh(db, PatientWallet, wallet.id).balance == 0


def test_definite_rejection_is_compensated(db, client, monkeypatch):
    monkeypatch.setattr(PaystackService, "initiate_refund",
                        lambda self, ref, amount=None: {"status": False, "message": "declined"})
    admin, patient, wallet, tx, req = _deposit_refund_setup(db)
    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 502
    assert _fresh(db, PatientWallet, wallet.id).balance == Decimal("1000")
    assert _fresh(db, PatientRefundRequest, req.id).status == "pending"
    assert [t.status for t in _refund_txs(db)] == ["failed"]


def test_ambiguous_error_stays_processing_and_is_never_retried(db, client, monkeypatch):
    calls = []

    def boom(self, ref, amount=None):
        calls.append(ref)
        return {"status": False, "ambiguous": True, "message": "Network error: timeout"}

    monkeypatch.setattr(PaystackService, "initiate_refund", boom)
    admin, patient, wallet, tx, req = _deposit_refund_setup(db)
    h = auth_headers(admin)
    r = client.put(_url(req), json={"action": "approve"}, headers=h)
    assert r.status_code == 502
    assert _fresh(db, PatientRefundRequest, req.id).status == "processing"
    assert _fresh(db, PatientWallet, wallet.id).balance == 0  # still debited: money may have left
    assert [t.status for t in _refund_txs(db)] == ["pending"]
    # no second attempt, no reject, no double credit
    assert client.put(_url(req), json={"action": "approve"}, headers=h).status_code == 409
    assert client.put(_url(req), json={"action": "reject"}, headers=h).status_code == 409
    assert len(calls) == 1
    assert _fresh(db, PatientWallet, wallet.id).balance == 0


# --- refund of a refund ---------------------------------------------------------

def test_cannot_request_refund_of_a_refund(db, client):
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    refund_tx = _tx(db, wallet, patient, "500", type_="refund")
    r = client.post("/api/v1/patient/wallet/refund/request", headers=auth_headers(patient.user),
                    json={"transaction_id": str(refund_tx.id), "amount": "500", "reason": "give me more money"})
    assert r.status_code == 400
    assert db.query(PatientRefundRequest).count() == 0


def test_approval_refuses_refund_of_a_refund(db, client, monkeypatch):
    called = []
    monkeypatch.setattr(PaystackService, "initiate_refund",
                        lambda self, ref, amount=None: called.append(ref) or {"status": True, "data": {"id": 1}})
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    refund_tx = _tx(db, wallet, patient, "500", reference="999", type_="refund")
    req = _request(db, patient, refund_tx, "500")
    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 409
    assert called == [] and _fresh(db, PatientWallet, wallet.id).balance == 0


def test_approval_refuses_unsuccessful_original(db, client):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    tx = _tx(db, wallet, patient, "500")
    tx.status = "failed"
    db.commit()
    req = _request(db, patient, tx, "500")
    assert client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin)).status_code == 409


# --- refund webhooks --------------------------------------------------------------

@pytest.fixture
def approved_card_refund(db, client, monkeypatch):
    monkeypatch.setattr(PaystackService, "initiate_refund",
                        lambda self, ref, amount=None: {"status": True, "data": {"id": 777}})
    admin, patient, wallet, tx, req = _deposit_refund_setup(db)
    assert client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin)).status_code == 200
    return wallet, req


def test_refund_processed_webhook_finalises(db, client, approved_card_refund):
    wallet, req = approved_card_refund
    for _ in range(2):  # replay safe
        assert _event(client, "refund.processed", {"id": 777, "status": "processed"}).status_code == 200
    assert [t.status for t in _refund_txs(db)] == ["successful"]
    assert _fresh(db, PatientWallet, wallet.id).balance == 0


def test_refund_failed_webhook_recredits_wallet_once(db, client, approved_card_refund):
    wallet, req = approved_card_refund
    for _ in range(2):  # replay must not credit twice
        assert _event(client, "refund.failed", {"id": 777, "status": "failed"}).status_code == 200
    assert [t.status for t in _refund_txs(db)] == ["failed"]
    assert _fresh(db, PatientWallet, wallet.id).balance == Decimal("1000")
    assert _fresh(db, PatientRefundRequest, req.id).status == "failed"


def test_refund_webhook_for_unknown_id_is_ignored(db, client):
    assert _event(client, "refund.failed", {"id": 424242}).status_code == 200


# --- prescriptions ------------------------------------------------------------------

def _rx(db, cost=2500.0, status="pending"):
    patient = make_patient(db)
    rx = Prescription(patient_id=patient.id, requested_by_user_id=patient.user_id, medication_name="X",
                      delivery_option="Pickup", address="1 Test Close", total_cost=cost,
                      paystack_reference=f"RX_{uuid.uuid4().hex[:8]}", payment_status=status)
    db.add(rx)
    db.commit()
    return rx


def _rx_status(db, rx):
    db.expire_all()
    return db.get(Prescription, rx.id).payment_status


def test_prescription_paid_with_correct_amount_and_replay_safe(db, client):
    rx = _rx(db)
    for _ in range(2):
        assert _event(client, "charge.success",
                      {"reference": rx.paystack_reference, "amount": 250000, "status": "success"}).status_code == 200
    assert _rx_status(db, rx) == "paid"


def test_prescription_wrong_amount_not_marked_paid(db, client):
    rx = _rx(db)
    _event(client, "charge.success", {"reference": rx.paystack_reference, "amount": 100, "status": "success"})
    assert _rx_status(db, rx) == "pending"


def test_prescription_failed_after_paid_stays_paid(db, client):
    rx = _rx(db, status="paid")
    _event(client, "charge.failed", {"reference": rx.paystack_reference, "amount": 250000, "status": "failed"})
    assert _rx_status(db, rx) == "paid"
