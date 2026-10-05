"""POST /patient/wallet/verify-payment is scoped to the caller's own transactions."""

import uuid
from decimal import Decimal

from app.core.money import to_kobo
from app.db.models.patient import PatientTransaction, PatientWallet
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_patient

URL = "/api/v1/patient/wallet/verify-payment"


def _deposit(db, patient, amount="5000"):
    wallet = PatientWallet(patient_id=patient.id, balance_kobo=0)
    db.add(wallet)
    db.commit()
    tx = PatientTransaction(id=uuid.uuid4(), patient_id=patient.id, wallet_id=wallet.id, type="deposit",
                            amount_kobo=to_kobo(amount), status="pending", paystack_reference=f"DEP_{uuid.uuid4().hex[:8]}")
    db.add(tx)
    db.commit()
    return wallet, tx


def _mock(monkeypatch, calls):
    def fake(self, ref):
        calls.append(ref)
        return {"status": True, "data": {"status": "success", "amount_kobo": 500000, "channel": "card"}}
    monkeypatch.setattr(PaystackService, "verify_transaction", fake)


def test_owner_can_verify(db, client, monkeypatch):
    calls = []
    _mock(monkeypatch, calls)
    patient = make_patient(db)
    wallet, tx = _deposit(db, patient)
    r = client.post(URL, json={"reference": tx.paystack_reference}, headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    assert r.json()["transaction_id"] == str(tx.id)


def test_other_patient_gets_404_and_nothing_processed(db, client, monkeypatch):
    calls = []
    _mock(monkeypatch, calls)
    victim = make_patient(db)
    wallet, tx = _deposit(db, victim)
    attacker = make_patient(db)
    r = client.post(URL, json={"reference": tx.paystack_reference}, headers=auth_headers(attacker.user))
    assert r.status_code == 404
    assert str(tx.id) not in r.text
    assert calls == []
    db.expire_all()
    assert db.get(PatientTransaction, tx.id).status == "pending"


def test_unknown_reference_has_same_404(db, client, monkeypatch):
    _mock(monkeypatch, [])
    patient = make_patient(db)
    victim = make_patient(db)
    _, tx = _deposit(db, victim)
    own = client.post(URL, json={"reference": "DEP_nope"}, headers=auth_headers(patient.user))
    foreign = client.post(URL, json={"reference": tx.paystack_reference}, headers=auth_headers(patient.user))
    assert own.status_code == foreign.status_code == 404
    assert own.json() == foreign.json()
