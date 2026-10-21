"""The legacy refund-request routes are retired (Refunds v2): they answer 410 and move no money. The admin check stays in front
of the 410, and a patient can still read their old requests."""

import uuid

import pytest

from app.core.money import to_kobo
from app.db.models.patient import PatientRefundRequest, PatientTransaction, PatientWallet
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_admin, make_patient, make_user

PATIENT_GONE = "Refunds are now handled by the Oakly team. Contact support to request one."
ADMIN_MOVED = "This route has moved to /api/v1/admin/refunds."
LIST_URL = "/api/v1/patient/admin/refund/requests"


def _wallet(db, patient, balance="0"):
    w = PatientWallet(patient_id=patient.id, balance_kobo=to_kobo(balance))
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _tx(db, wallet, patient, amount="1000", reference=None, type_="payment"):
    t = PatientTransaction(
        id=uuid.uuid4(), patient_id=patient.id, wallet_id=wallet.id, type=type_,
        amount_kobo=to_kobo(amount), status="successful", paystack_reference=reference,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _request(db, patient, tx, amount="1000"):
    r = PatientRefundRequest(
        id=uuid.uuid4(), patient_id=patient.id, transaction_id=tx.id,
        amount_kobo=to_kobo(amount), reason="Service was not delivered", status="pending",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _url(req):
    return f"{LIST_URL}/{req.id}"


def _fresh(db, model, pk):
    db.expire_all()
    return db.get(model, pk)


@pytest.fixture
def paystack_refunds(monkeypatch):
    """Every call the old code path would make to Paystack: it must stay empty."""
    calls = []

    def fake(self, reference, amount=None):
        calls.append((reference, amount))
        return {"status": True, "data": {"id": 987654}}

    monkeypatch.setattr(PaystackService, "initiate_refund", fake)
    return calls


def test_refund_endpoints_require_auth(db, client):
    patient = make_patient(db)
    tx = _tx(db, _wallet(db, patient), patient)
    req = _request(db, patient, tx)
    assert client.put(_url(req), json={"action": "approve"}).status_code == 401
    assert client.get(LIST_URL).status_code == 401
    assert client.post("/api/v1/patient/wallet/refund/request", json={}).status_code == 401


def test_non_admin_cannot_approve(db, client, paystack_refunds):
    patient = make_patient(db)
    tx = _tx(db, _wallet(db, patient), patient)
    req = _request(db, patient, tx)
    r = client.put(_url(req), json={"action": "approve"}, headers=auth_headers(patient.user))
    assert r.status_code == 401
    assert _fresh(db, PatientRefundRequest, req.id).status == "pending"
    assert paystack_refunds == []


def test_non_admin_cannot_list(db, client):
    patient = make_patient(db)
    assert client.get(LIST_URL, headers=auth_headers(patient.user)).status_code == 401


def test_admin_without_refund_permission_is_forbidden(db, client):
    assert client.get(LIST_URL, headers=auth_headers(make_admin(db, ["support"]))).status_code == 403


def test_user_type_admin_is_not_an_admin(db, client):
    fake_admin = make_user(db, "admin")
    assert client.get(LIST_URL, headers=auth_headers(fake_admin)).status_code == 401


def test_admin_list_is_410_pointing_at_the_new_route(db, client):
    for roles in (["super_admin"], ["finance"]):
        r = client.get(LIST_URL, headers=auth_headers(make_admin(db, roles)))
        assert r.status_code == 410 and r.json() == {"detail": ADMIN_MOVED}, roles


def test_approve_and_reject_are_410_and_pay_nothing(db, client, paystack_refunds):
    admin = make_admin(db)
    patient = make_patient(db)
    wallet = _wallet(db, patient, "0")
    tx = _tx(db, wallet, patient, "1000", reference="PAY_legacy")
    req = _request(db, patient, tx)
    for action in ("approve", "reject"):
        r = client.put(_url(req), json={"action": action}, headers=auth_headers(admin))
        assert r.status_code == 410 and r.json() == {"detail": ADMIN_MOVED}
    assert paystack_refunds == []
    assert _fresh(db, PatientRefundRequest, req.id).status == "pending"
    assert _fresh(db, PatientWallet, wallet.id).balance_kobo == 0
    assert db.query(PatientTransaction).filter_by(type="refund").count() == 0


def test_unknown_request_is_410_too(db, client):
    r = client.put(f"{LIST_URL}/{uuid.uuid4()}", json={"action": "approve"}, headers=auth_headers(make_admin(db)))
    assert r.status_code == 410


def test_patient_refund_request_is_410_and_creates_nothing(db, client):
    patient = make_patient(db)
    tx = _tx(db, _wallet(db, patient), patient)
    r = client.post("/api/v1/patient/wallet/refund/request", headers=auth_headers(patient.user),
                    json={"transaction_id": str(tx.id), "amount": "100", "reason": "Service was not delivered"})
    assert r.status_code == 410 and r.json() == {"detail": PATIENT_GONE}
    assert db.query(PatientRefundRequest).count() == 0


def test_patient_can_still_read_their_old_requests(db, client):
    patient = make_patient(db)
    tx = _tx(db, _wallet(db, patient), patient)
    req = _request(db, patient, tx, "250")
    r = client.get("/api/v1/patient/wallet/refund/requests", headers=auth_headers(patient.user))
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [str(req.id)]
