"""A payout account whose holder does not look like the caregiver is saved but held: Finance reads both names and approves or
rejects it (``/admin/payouts/bank-reviews``). Until it is approved no payout is made to it."""

from datetime import datetime

import pytest

from app.db.models.audit import AdminAuditLog
from app.db.models.caregiver import CaregiverBankAccount, CaregiverWallet
from app.services import admin_notify, bank_accounts, payouts
from app.tests.factories import auth_headers, make_admin, make_caregiver

ACCOUNT = "/api/v1/caregiver/bank-account"
REVIEWS = "/api/v1/admin/payouts/bank-reviews"
NOW = datetime(2026, 10, 12, 6, 10)


@pytest.fixture(autouse=True)
def _fresh_state(fake_paystack):
    bank_accounts.clear_bank_cache()
    bank_accounts.clear_throttle()
    yield
    bank_accounts.clear_bank_cache()
    bank_accounts.clear_throttle()


def _admin(db, role):
    return auth_headers(make_admin(db, [role], password=None))


def _flagged(db, client, fake_paystack, *, profile="Ada Okafor", holder="CHIDI OBI", number="0123456789", balance=1_000_000):
    """A caregiver with money whose saved account is held for review."""
    cg = make_caregiver(db, full_name=profile)
    db.add(CaregiverWallet(caregiver_id=cg.id, balance=balance))
    db.commit()
    fake_paystack.accounts[(number, "058")] = holder
    r = client.put(ACCOUNT, json={"account_number": number, "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 200 and r.json()["name_review_status"] == "pending", r.text
    return cg


def _decide(client, cg, verb, headers, *, last4="6789", reason="Checked with the caregiver by phone"):
    return client.post(f"{REVIEWS}/{cg.id}/{verb}", json={"reason": reason, "account_last4": last4}, headers=headers)


def _wallet(db, cg):
    db.expire_all()
    return db.query(CaregiverBankAccount).filter_by(caregiver_id=cg.id).one()


# --- the queue ---------------------------------------------------------------------------------------------------------


def test_the_queue_shows_both_names_to_those_who_may_read_payments(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    r = client.get(REVIEWS, headers=_admin(db, "finance"))
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert r.json()["total"] == 1
    assert item["caregiver_id"] == str(cg.id) and item["caregiver_name"] == "Ada Okafor" and item["account_name"] == "CHIDI OBI"
    assert item["bank_name"] == "Placeholder Bank" and item["account_number"] == "******6789"
    assert item["status"] == "pending" and item["balance_kobo"] == 1_000_000
    assert "0123456789" not in r.text  # the number is masked


def test_the_queue_needs_a_permission_and_a_login(db, client, fake_paystack):
    _flagged(db, client, fake_paystack)
    assert client.get(REVIEWS).status_code == 401
    assert client.get(REVIEWS, headers=_admin(db, "support")).status_code == 403


def test_the_queue_lists_pending_by_default_and_rejected_on_request(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    fin = _admin(db, "finance")
    assert _decide(client, cg, "reject", fin).status_code == 200
    assert client.get(REVIEWS, headers=fin).json()["total"] == 0
    assert client.get(REVIEWS, params={"status": "rejected"}, headers=fin).json()["total"] == 1


# --- deciding ----------------------------------------------------------------------------------------------------------


def test_approving_releases_the_hold_and_is_audited_with_no_account_number(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    assert bank_accounts.is_on_name_hold(_wallet(db, cg))
    fin = _admin(db, "finance")
    submitted = client.get(REVIEWS, headers=fin).json()["items"][0]["submitted_at"]
    r = _decide(client, cg, "approve", fin)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert r.json()["submitted_at"] == submitted and r.json()["decided_at"] is not None  # a decision is not a new save
    row = _wallet(db, cg)
    assert row.name_review_status == "approved" and row.name_review_at is not None
    assert not bank_accounts.is_on_name_hold(row)
    audit = db.query(AdminAuditLog).filter_by(action="bank_account.name_approve").one()
    assert audit.entity_id == str(cg.id) and audit.reason == "Checked with the caregiver by phone"
    assert "0123456789" not in str(audit.before) + str(audit.after)


def test_the_caregiver_sees_the_real_name_only_once_the_account_is_approved(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack, holder="CHIDI OBI")
    h = auth_headers(cg.user)
    assert client.get(ACCOUNT, headers=h).json()["account_name"] == bank_accounts.mask_name("CHIDI OBI")
    assert _decide(client, cg, "approve", _admin(db, "finance")).status_code == 200
    assert client.get(ACCOUNT, headers=h).json()["account_name"] == "CHIDI OBI"


def test_rejecting_keeps_the_hold_and_the_name_masked(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    assert _decide(client, cg, "reject", _admin(db, "finance")).status_code == 200
    row = _wallet(db, cg)
    assert row.name_review_status == "rejected" and bank_accounts.is_on_name_hold(row)
    assert db.query(AdminAuditLog).filter_by(action="bank_account.name_reject").count() == 1
    assert "CHIDI OBI" not in client.get(ACCOUNT, headers=auth_headers(cg.user)).text


def test_only_someone_who_may_execute_payouts_decides(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    for role in ("support", "operations", "verification"):
        assert _decide(client, cg, "approve", _admin(db, role)).status_code == 403, role
    assert _wallet(db, cg).name_review_status == "pending"


def test_a_decision_needs_a_reason(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    r = client.post(f"{REVIEWS}/{cg.id}/approve", json={"account_last4": "6789"}, headers=_admin(db, "finance"))
    assert r.status_code == 422


def test_a_decision_on_a_changed_account_is_refused(db, client, fake_paystack):
    """The reviewer looked at one account; the caregiver then saved another. The decision must not carry over."""
    cg = _flagged(db, client, fake_paystack)
    fake_paystack.accounts[("0987654321", "058")] = "SOMEONE ELSE"
    client.put(ACCOUNT, json={"account_number": "0987654321", "bank_code": "058"}, headers=auth_headers(cg.user))
    r = _decide(client, cg, "approve", _admin(db, "finance"), last4="6789")
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert _wallet(db, cg).name_review_status == "pending"


def test_a_decision_is_refused_when_nothing_is_waiting(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    fin = _admin(db, "finance")
    assert _decide(client, cg, "approve", fin).status_code == 200
    again = _decide(client, cg, "reject", fin)
    assert again.status_code == 409 and again.json()["detail"] == "This account is not waiting for a name review"
    matching = make_caregiver(db, full_name="Ada Okafor")
    fake_paystack.accounts[("1111111111", "058")] = "ADA OKAFOR"
    client.put(ACCOUNT, json={"account_number": "1111111111", "bank_code": "058"}, headers=auth_headers(matching.user))
    assert _decide(client, matching, "approve", fin, last4="1111").status_code == 409  # a clean account has nothing to decide


def test_an_unknown_caregiver_is_404(db, client):
    import uuid
    r = client.post(f"{REVIEWS}/{uuid.uuid4()}/approve", json={"reason": "x", "account_last4": "6789"},
                    headers=_admin(db, "finance"))
    assert r.status_code == 404


# --- payouts -----------------------------------------------------------------------------------------------------------


def _payout_rows(db, cg):
    from app.db.models.payout import Payout
    db.expire_all()
    return db.query(Payout).filter(Payout.caregiver_id == cg.id).count() > 0


@pytest.mark.parametrize("verb, paid", [("none", False), ("approve", True), ("reject", False)])
def test_an_account_held_for_review_is_not_paid_until_approved(db, client, fake_paystack, verb, paid):
    cg = _flagged(db, client, fake_paystack)
    # the 24 hour pause of a first account with money in the wallet is not what is under test
    db.query(CaregiverBankAccount).filter_by(caregiver_id=cg.id).update({"payout_blocked_until": None})
    db.commit()
    if verb != "none":
        assert _decide(client, cg, verb, _admin(db, "finance")).status_code == 200
    payouts.create_batch(db, period_key="2026-10-12", now=NOW)
    assert _payout_rows(db, cg) is paid
    assert (payouts._eligibility_problem(db, cg.id, NOW) is None) is paid


def test_the_reason_a_held_account_is_not_paid_names_the_review(db, client, fake_paystack):
    cg = _flagged(db, client, fake_paystack)
    db.query(CaregiverBankAccount).filter_by(caregiver_id=cg.id).update({"payout_blocked_until": None})
    db.commit()
    assert "name" in payouts._eligibility_problem(db, cg.id, NOW)


# --- telling Finance -----------------------------------------------------------------------------------------------------


def test_a_flagged_account_tells_the_admins_after_the_save_and_a_match_does_not(db, client, fake_paystack, monkeypatch):
    calls = []
    monkeypatch.setattr(admin_notify, "notify", lambda event, **kw: calls.append((event, kw)))
    cg = _flagged(db, client, fake_paystack)
    assert calls == [("bank_name_review", {"caregiver_id": cg.id})]
    ok = make_caregiver(db, full_name="Ada Okafor")
    fake_paystack.accounts[("2222222222", "058")] = "ADA OKAFOR"
    client.put(ACCOUNT, json={"account_number": "2222222222", "bank_code": "058"}, headers=auth_headers(ok.user))
    assert len(calls) == 1
