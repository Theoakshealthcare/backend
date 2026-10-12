"""POST /caregiver/wallet/withdraw is gone (A3 slice 2): it answers 410 with a message, whoever calls and whatever they send."""

from sqlalchemy import select

from app.db.models.caregiver import CaregiverTransaction, CaregiverWallet
from app.tests.factories import auth_headers, make_caregiver, make_caregiver_wallet, make_patient

URL = "/api/v1/caregiver/wallet/withdraw"


def test_requires_auth(client):
    assert client.post(URL, json={"amount": 100000}).status_code == 401


def test_a_patient_is_still_refused(db, client):
    patient = make_patient(db)
    assert client.post(URL, json={"amount": 100000}, headers=auth_headers(patient.user)).status_code == 403


def test_a_caregiver_gets_410_and_nothing_moves(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=500_000)
    for body in ({"amount": 100000}, {"amount": -5}, {}):
        r = client.post(URL, json=body, headers=auth_headers(cg.user))
        assert r.status_code == 410, body
        assert "no longer available" in r.json()["detail"]
    db.expire_all()
    assert db.execute(select(CaregiverWallet.balance)).scalar_one() == 500_000
    assert db.query(CaregiverTransaction).count() == 0
