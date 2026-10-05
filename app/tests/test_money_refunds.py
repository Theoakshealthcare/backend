"""Admin refund approval: one destination, no double approval, real admin check."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.models.patient import PatientRefundRequest, PatientTransaction, PatientWallet
from app.main import app as fastapi_app
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_admin, make_patient, make_user


def _wallet(db, patient, balance="0"):
    w = PatientWallet(patient_id=patient.id, balance=Decimal(balance))
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _tx(db, wallet, patient, amount="1000", reference=None, type_="payment"):
    t = PatientTransaction(
        id=uuid.uuid4(), patient_id=patient.id, wallet_id=wallet.id, type=type_,
        amount=Decimal(amount), status="successful", paystack_reference=reference,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _request(db, patient, tx, amount="1000"):
    r = PatientRefundRequest(
        id=uuid.uuid4(), patient_id=patient.id, transaction_id=tx.id,
        amount=Decimal(amount), reason="Service was not delivered", status="pending",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _url(req):
    return f"/api/v1/patient/admin/refund/requests/{req.id}"


@pytest.fixture
def paystack_refunds(monkeypatch):
    calls = []

    def fake(self, reference, amount=None):
        calls.append((reference, amount))
        return {"status": True, "data": {"id": 987654}}

    monkeypatch.setattr(PaystackService, "initiate_refund", fake)
    return calls


def _fresh(db, model, pk):
    db.expire_all()
    return db.get(model, pk)


def test_refund_endpoints_require_auth(db, client):
    patient = make_patient(db)
    tx = _tx(db, _wallet(db, patient), patient)
    req = _request(db, patient, tx)
    assert client.put(_url(req), json={"action": "approve"}).status_code == 401
    assert client.get("/api/v1/patient/admin/refund/requests").status_code == 401


def test_non_admin_cannot_approve(db, client, paystack_refunds):
    patient = make_patient(db)
    tx = _tx(db, _wallet(db, patient), patient)
    req = _request(db, patient, tx)
    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(patient.user))
    assert r.status_code == 403
    assert _fresh(db, PatientRefundRequest, req.id).status == "pending"
    assert paystack_refunds == []


def test_non_admin_cannot_list(db, client):
    patient = make_patient(db)
    r = client.get("/api/v1/patient/admin/refund/requests", headers=auth_headers(patient.user))
    assert r.status_code == 403


def test_admin_row_authorises_regardless_of_user_type(db, client):
    # the old check compared user_type == "admin"; admins are identified by an Admin row
    admin = make_admin(db)
    r = client.get("/api/v1/patient/admin/refund/requests", headers=auth_headers(admin))
    assert r.status_code == 200


def test_user_type_admin_without_admin_row_is_rejected(db, client):
    fake_admin = make_user(db, "admin")
    r = client.get("/api/v1/patient/admin/refund/requests", headers=auth_headers(fake_admin))
    assert r.status_code == 403


def test_paystack_original_refunds_to_paystack_only(db, client, paystack_refunds):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "1000")
    tx = _tx(db, wallet, patient, "1000", reference="DEP_x_1", type_="deposit")
    req = _request(db, patient, tx, "1000")

    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    assert paystack_refunds == [("DEP_x_1", Decimal("1000"))]
    w = _fresh(db, PatientWallet, wallet.id)
    # the deposit is reversed out of the wallet and paid back to the card: never both
    assert w.balance == 0 and w.total_refunded == 0
    assert _fresh(db, PatientRefundRequest, req.id).status == "approved"


def test_wallet_original_refunds_to_wallet_only(db, client, paystack_refunds):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    tx = _tx(db, wallet, patient, "1000", reference=None)
    req = _request(db, patient, tx, "400")

    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    assert paystack_refunds == []
    w = _fresh(db, PatientWallet, wallet.id)
    assert w.balance == Decimal("400") and w.total_refunded == Decimal("400")


def test_paystack_failure_leaves_request_pending_and_pays_nothing(db, client, monkeypatch):
    monkeypatch.setattr(
        PaystackService, "initiate_refund",
        lambda self, ref, amount=None: {"status": False, "message": "boom"},
    )
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "1000")
    tx = _tx(db, wallet, patient, "1000", reference="DEP_x_2", type_="deposit")
    req = _request(db, patient, tx)

    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 502
    assert _fresh(db, PatientRefundRequest, req.id).status == "pending"
    assert _fresh(db, PatientWallet, wallet.id).balance == Decimal("1000")
    # a definite rejection is compensated: the wallet debit is reversed, the refund tx is marked failed
    assert [t.status for t in db.query(PatientTransaction).filter_by(type="refund")] in ([], ["failed"])


def test_deposit_refund_blocked_when_wallet_already_spent(db, client, paystack_refunds):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "200")  # most of the deposit was spent
    tx = _tx(db, wallet, patient, "1000", reference="DEP_x_4", type_="deposit")
    req = _request(db, patient, tx, "1000")
    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 409
    assert paystack_refunds == []
    assert _fresh(db, PatientRefundRequest, req.id).status == "pending"


def test_second_approval_is_rejected(db, client, paystack_refunds):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    tx = _tx(db, wallet, patient, "1000")
    req = _request(db, patient, tx, "1000")

    assert client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin)).status_code == 200
    r2 = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert r2.status_code == 409
    assert _fresh(db, PatientWallet, wallet.id).balance == Decimal("1000")
    assert db.query(PatientTransaction).filter_by(type="refund").count() == 1


def test_unknown_request_is_404(db, client):
    admin = make_admin(db)
    r = client.put(f"/api/v1/patient/admin/refund/requests/{uuid.uuid4()}", json={"action": "approve"},
                   headers=auth_headers(admin))
    assert r.status_code == 404


def test_reject_does_not_pay(db, client, paystack_refunds):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    tx = _tx(db, wallet, patient, "1000")
    req = _request(db, patient, tx)
    r = client.put(_url(req), json={"action": "reject", "admin_notes": "no"}, headers=auth_headers(admin))
    assert r.status_code == 200
    assert _fresh(db, PatientRefundRequest, req.id).status == "rejected"
    assert _fresh(db, PatientWallet, wallet.id).balance == 0
    assert paystack_refunds == []


def test_concurrent_double_approval_pays_once(db, monkeypatch):
    calls = []

    def slow(self, reference, amount=None):
        calls.append(reference)
        time.sleep(0.5)  # widen the race window
        return {"status": True, "data": {"id": 1}}

    monkeypatch.setattr(PaystackService, "initiate_refund", slow)
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "1000")
    tx = _tx(db, wallet, patient, "1000", reference="DEP_x_3", type_="deposit")
    req = _request(db, patient, tx)
    headers = auth_headers(admin)

    def approve():
        c = TestClient(fastapi_app, raise_server_exceptions=False)
        return c.put(_url(req), json={"action": "approve"}, headers=headers)

    with ThreadPoolExecutor(2) as ex:
        futures = [ex.submit(approve) for _ in range(2)]
        codes = sorted(f.result(timeout=30).status_code for f in futures)
    assert codes == [200, 409]
    assert len(calls) == 1


def test_cumulative_refunds_cannot_exceed_original_amount(db, client, paystack_refunds):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    tx = _tx(db, wallet, patient, "1000")
    first = _request(db, patient, tx, "800")
    assert client.put(_url(first), json={"action": "approve"}, headers=auth_headers(admin)).status_code == 200
    second = _request(db, patient, tx, "800")
    r = client.put(_url(second), json={"action": "approve"}, headers=auth_headers(admin))
    assert r.status_code == 409
    assert _fresh(db, PatientWallet, wallet.id).balance == Decimal("800")
