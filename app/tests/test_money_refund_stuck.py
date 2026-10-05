"""A refund stuck in 'processing' (gateway id never stored) is closed by webhook fallback matching."""

from decimal import Decimal

import pytest

from app.db.models.patient import PatientRefundRequest, PatientWallet
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers
from app.tests.test_money_refund_flow import _deposit_refund_setup, _event, _refund_txs
from app.tests.test_money_refunds import _fresh, _url


@pytest.fixture
def ambiguous_refund(db, client, monkeypatch):
    monkeypatch.setattr(PaystackService, "initiate_refund",
                        lambda self, ref, amount=None: {"status": False, "ambiguous": True, "message": "timeout"})
    admin, patient, wallet, tx, req = _deposit_refund_setup(db)
    assert client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin)).status_code == 502
    return wallet, tx, req


def _data(tx, rid=888, amount=100000):
    return {"id": rid, "transaction_reference": tx.paystack_reference, "amount": amount}


def test_ambiguous_then_processed_is_closed_by_reference_match(db, client, ambiguous_refund):
    wallet, tx, req = ambiguous_refund
    for _ in range(2):
        assert _event(client, "refund.processed", _data(tx)).status_code == 200
    assert _fresh(db, PatientRefundRequest, req.id).status == "approved"
    (rt,) = _refund_txs(db)
    assert rt.status == "successful" and rt.paystack_reference == "888"
    assert _fresh(db, PatientWallet, wallet.id).balance_kobo == 0


def test_ambiguous_then_failed_recredits_deposit_once(db, client, ambiguous_refund):
    wallet, tx, req = ambiguous_refund
    for _ in range(3):  # replay
        assert _event(client, "refund.failed", _data(tx)).status_code == 200
    assert _fresh(db, PatientWallet, wallet.id).balance_kobo == 100000
    assert _fresh(db, PatientRefundRequest, req.id).status == "failed"
    assert [t.status for t in _refund_txs(db)] == ["failed"]


def test_fallback_needs_matching_amount_and_logs_error(db, client, ambiguous_refund, caplog):
    wallet, tx, req = ambiguous_refund
    assert _event(client, "refund.failed", _data(tx, amount=5)).status_code == 200
    assert _fresh(db, PatientRefundRequest, req.id).status == "processing"
    assert _fresh(db, PatientWallet, wallet.id).balance_kobo == 0
    assert any(tx.paystack_reference in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


def test_late_finish_does_not_reopen_a_failed_refund(db, client, monkeypatch):
    """refund.failed arriving before phase 3 commits must not be undone by phase 3."""
    admin, patient, wallet, tx, req = _deposit_refund_setup(db)

    def fail_then_succeed(self, ref, amount=None):
        assert _event(client, "refund.failed", _data(tx, rid=999)).status_code == 200
        return {"status": True, "data": {"id": 999}}

    monkeypatch.setattr(PaystackService, "initiate_refund", fail_then_succeed)
    client.put(_url(req), json={"action": "approve"}, headers=auth_headers(admin))
    assert _fresh(db, PatientRefundRequest, req.id).status == "failed"
    assert _fresh(db, PatientWallet, wallet.id).balance_kobo == 100000
