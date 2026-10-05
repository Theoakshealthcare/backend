"""Caregiver withdrawals: input validation and no-overdraw under concurrency."""

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.config import settings
from app.db.models.caregiver import CaregiverTransaction, CaregiverWallet
from app.db.session import SessionLocal
from app.main import app as fastapi_app
from app.tests.factories import auth_headers, make_caregiver, make_caregiver_wallet, make_patient

URL = "/api/v1/caregiver/wallet/withdraw"


def _withdraw(headers, amount):
    # a fresh client per call so threads never share a portal
    c = TestClient(fastapi_app, raise_server_exceptions=False)
    return c.post(URL, json={"amount": amount}, headers=headers)


def _balance(db):
    db.expire_all()
    return db.execute(select(CaregiverWallet.balance)).scalar_one()


def _tx_count(db):
    db.expire_all()
    return db.query(CaregiverTransaction).count()


def test_withdraw_requires_auth(client):
    assert client.post(URL, json={"amount": 100000}).status_code == 401


def test_withdraw_rejects_patient(db, client):
    patient = make_patient(db)
    r = client.post(URL, json={"amount": 100000}, headers=auth_headers(patient.user))
    assert r.status_code == 403


def test_withdraw_happy_path(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=500000)
    amount = settings.MIN_WITHDRAWAL_KOBO
    r = client.post(URL, json={"amount": amount}, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert _balance(db) == 500000 - amount
    assert _tx_count(db) == 1


def test_withdraw_rejects_negative_amount(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=500000)
    r = client.post(URL, json={"amount": -500000}, headers=auth_headers(cg.user))
    assert r.status_code in (400, 422)
    assert _balance(db) == 500000  # a negative withdrawal must never credit the wallet
    assert _tx_count(db) == 0


def test_withdraw_rejects_zero_amount(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=500000)
    r = client.post(URL, json={"amount": 0}, headers=auth_headers(cg.user))
    assert r.status_code in (400, 422)
    assert _balance(db) == 500000
    assert _tx_count(db) == 0


def test_withdraw_rejects_below_minimum(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=500000)
    r = client.post(URL, json={"amount": settings.MIN_WITHDRAWAL_KOBO - 1}, headers=auth_headers(cg.user))
    assert r.status_code == 400
    assert "minimum" in r.json()["detail"].lower()
    assert _balance(db) == 500000


def test_withdraw_rejects_overdraw(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=200000)
    r = client.post(URL, json={"amount": 200001}, headers=auth_headers(cg.user))
    assert r.status_code == 400
    assert r.json()["detail"] == "Insufficient wallet balance."
    assert _balance(db) == 200000


def test_concurrent_double_submit_cannot_overdraw(db):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=1_000_000)
    headers = auth_headers(cg.user)
    with ThreadPoolExecutor(2) as ex:
        futures = [ex.submit(_withdraw, headers, 800_000) for _ in range(2)]
        codes = sorted(f.result(timeout=30).status_code for f in futures)
    assert codes == [200, 400]
    assert _balance(db) == 200_000
    assert _tx_count(db) == 1


def test_withdraw_waits_on_wallet_row_lock_then_rechecks_balance(db):
    """Deterministic proof of the lock: while another transaction holds the wallet row,
    the withdrawal must block, and once released it must see the new (lower) balance."""
    cg = make_caregiver(db)
    wallet = make_caregiver_wallet(db, cg, balance_kobo=500_000)
    headers = auth_headers(cg.user)

    locker = SessionLocal()
    try:
        locker.execute(select(CaregiverWallet).where(CaregiverWallet.id == wallet.id).with_for_update())
        with ThreadPoolExecutor(1) as ex:
            fut = ex.submit(_withdraw, headers, 400_000)
            time.sleep(1.0)
            assert not fut.done(), "withdrawal did not wait for the wallet row lock"
            locker.execute(update(CaregiverWallet).where(CaregiverWallet.id == wallet.id).values(balance=0))
            locker.commit()
            r = fut.result(timeout=30)
    finally:
        locker.close()
    assert r.status_code == 400
    assert _balance(db) == 0
    assert _tx_count(db) == 0
