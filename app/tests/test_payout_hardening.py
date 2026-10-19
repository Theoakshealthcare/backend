"""Payout hardening after the money review: a transfer that succeeds after we already returned the money, attempts that
outlive a retry, reconcile never releasing money on silence, a send-time eligibility check, and the admin ``resolve`` and
batch ``cancel`` actions. The invariant under test throughout: money is never paid twice without a person being told."""

import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from app.db.models.audit import AdminAuditLog
from app.db.models.caregiver import CaregiverBankAccount, CaregiverTransaction, CaregiverWallet
from app.db.models.payout import Payout, PayoutAttempt, PayoutBatch
from app.db.session import SessionLocal
from app.services import payouts
from app.tests.factories import auth_headers, make_admin
from app.tests.payout_helpers import (
    WEBHOOK, assert_never_over, assert_overpayment_on_record, cancelled_unsent_kobo, ledger, money_state, payable, payouts_of,
    signed, wallet_balance,
)
from app.tests.test_payouts import ADMIN, MONDAY, approve, make_batch

BOSS = ["super_admin"]


def approved_payout(db, fake, balance=1_000_000):
    cg = payable(db, balance, fake=fake)
    approve(db, make_batch(db))
    return cg, payouts_of(db, cg)[0]


def webhook(client, fake, reference, outcome):
    return client.post(WEBHOOK, **signed(fake.settle_transfer(reference, outcome)))


def attempts_of(db, payout_id):
    db.expire_all()
    return db.query(PayoutAttempt).filter_by(payout_id=payout_id).order_by(PayoutAttempt.attempt).all()


def frozen(db, cg):
    db.expire_all()
    return db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one().payouts_frozen_at is not None


def frozen_reason(db, cg):
    db.expire_all()
    return db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one().payouts_frozen_reason


def set_balance(db, kobo):
    db.query(CaregiverWallet).update({"balance": kobo})
    db.commit()


def sent_then(db, client, fake, outcome):
    """An approved, sent payout that Paystack then settled as ``outcome`` through a webhook."""
    cg, p = approved_payout(db, fake)
    payouts.dispatch_due(db)
    webhook(client, fake, p.reference, outcome)
    return cg, payouts_of(db, cg)[0]


# --- 1. success after we returned the money ----------------------------------------------------------------------------


def test_success_after_the_money_was_returned_takes_it_back_freezes_and_needs_attention(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "failed")
    assert wallet_balance(db, cg) == 1_000_000
    assert webhook(client, fake_paystack, p.reference, "success").status_code == 200
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and q.shortfall_kobo == 0
    assert wallet_balance(db, cg) == 0, "the returned money must be taken back, or the next batch pays it again"
    assert ledger(db, cg)[0].status == "successful" and "after" in q.failure_reason
    assert [a.status for a in attempts_of(db, p.id)] == ["success"]
    assert_never_over(db, cg, 1_000_000)
    assert frozen(db, cg) and "Paystack paid" in frozen_reason(db, cg)


def test_when_the_balance_no_longer_covers_it_the_rest_is_recorded_as_a_shortfall(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    set_balance(db, 300_000)  # the caregiver has since been charged or paid something else
    webhook(client, fake_paystack, p.reference, "success")
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and q.shortfall_kobo == 700_000
    assert wallet_balance(db, cg) == 0 and ledger(db, cg)[0].status == "successful"
    assert "700000" in q.failure_reason


def test_a_repeated_late_success_takes_the_money_back_once(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    set_balance(db, 400_000)  # earnings arrived after the claw-back
    for _ in range(2):
        assert webhook(client, fake_paystack, p.reference, "success").status_code == 200
    assert wallet_balance(db, cg) == 400_000 and payouts_of(db, cg)[0].shortfall_kobo == 0


def test_a_caregiver_who_needs_attention_is_left_out_of_new_batches(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    set_balance(db, 500_000)
    assert frozen(db, cg)
    assert payouts.create_batch(db, period_key="2026-10-19", now=MONDAY + timedelta(days=7)) is None
    assert wallet_balance(db, cg) == 500_000 and len(payouts_of(db, cg)) == 1


def test_the_database_allows_a_live_payout_beside_a_needs_attention_one_but_not_two_live_ones(db, client, fake_paystack):
    """needs_attention is not in the one-live-payout index any more: the caregiver's freeze keeps new batches away."""
    from sqlalchemy.exc import IntegrityError
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    q = payouts_of(db, cg)[0]

    def clone(reference, tx_id):
        return Payout(batch_id=q.batch_id, caregiver_id=cg.id, transaction_id=tx_id, amount_kobo=1, status="pending_approval",
                      bank_name="x", bank_code="x", account_number="x", account_name="x", recipient_code="x", reference=reference)

    def tx(reference):
        row = CaregiverTransaction(caregiver_id=cg.id, type="payout", amount=1, status="pending_approval", reference=reference)
        db.add(row)
        db.flush()
        return row.id

    db.add(clone("po-dup-1", tx("po-dup-1")))
    db.flush()  # beside the needs_attention payout: allowed
    db.add(clone("po-dup-2", tx("po-dup-2")))
    with pytest.raises(IntegrityError):
        db.flush()  # a second live one: refused
    db.rollback()


def test_a_needs_attention_payout_cannot_be_retried(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    with pytest.raises(payouts.PayoutConflict, match="failed or reversed"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")


def test_concurrent_late_successes_take_the_money_back_once(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    reference, payout_id, tx_id = p.reference, p.id, p.transaction_id

    def apply():
        session = SessionLocal()
        try:
            return payouts.apply_transfer_event(session, reference, "success", amount_kobo=1_000_000)
        finally:
            session.close()

    locker = SessionLocal()
    try:
        locker.query(Payout).filter(Payout.id == payout_id).with_for_update().all()
        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(apply) for _ in range(3)]
            time.sleep(1.0)
            assert not any(f.done() for f in futures)
            locker.rollback()
            results = sorted(f.result(timeout=60) for f in futures)
    finally:
        locker.close()
    assert results == ["duplicate", "duplicate", "needs_attention"]
    assert wallet_balance(db, cg) == 0 and payouts_of(db, cg)[0].shortfall_kobo == 0


# --- 2. attempts -------------------------------------------------------------------------------------------------------


def test_every_send_has_an_attempt_row_and_a_retry_keeps_the_history(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    first = p.reference
    payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="topped up")
    q = payouts_of(db, cg)[0]
    rows = attempts_of(db, p.id)
    assert [(a.attempt, a.reference) for a in rows] == [(1, first), (2, q.reference)]
    assert rows[0].status == "failed" and rows[0].failure_reason and rows[1].status == "pending"
    assert q.reference.endswith("-2")
    verified = [r for r in fake_paystack.requests if r[0] == "GET" and "/transfer/verify/" in r[1]]
    assert verified and verified[-1][1].endswith(first), "retry must ask Paystack about the current attempt first"


def test_a_late_success_for_an_earlier_attempt_while_the_retry_is_reserved_needs_attention(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    first = p.reference
    payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="again")
    assert webhook(client, fake_paystack, first, "success").status_code == 200
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and q.shortfall_kobo == 0
    assert wallet_balance(db, cg) == 0, "the retry's reservation stands: one payment, one reservation"
    assert payouts.dispatch_due(db) == 0, "the second attempt must not be sent now that the first was paid"
    assert [a.status for a in attempts_of(db, p.id)] == ["success", "pending"]


def test_a_late_success_for_an_earlier_attempt_after_the_retry_failed_takes_the_money_back(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    first = p.reference
    payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="again")
    payouts.dispatch_due(db)
    second = payouts_of(db, cg)[0].reference
    webhook(client, fake_paystack, second, "failed")
    assert wallet_balance(db, cg) == 1_000_000
    webhook(client, fake_paystack, first, "success")
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and wallet_balance(db, cg) == 0 and q.shortfall_kobo == 0


def test_a_late_event_for_an_earlier_attempt_is_not_unknown(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    first = p.reference
    payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="again")
    assert payouts.apply_transfer_event(db, first, "failed", reason="late") == "duplicate"
    assert payouts.apply_transfer_event(db, "po-nobody-1", "failed") == "unknown_reference"
    assert payouts_of(db, cg)[0].status == "processing"  # the old attempt's failure does not touch the live one


def test_retry_is_refused_unless_paystack_says_the_transfer_failed(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    admin = make_admin(db, password=None)
    for paystack_says, message in [("pending", "still pending"), ("otp", "OTP")]:
        fake_paystack.transfers[p.reference]["status"] = paystack_says
        with pytest.raises(payouts.PayoutConflict, match=message):
            payouts.retry_payout(db, p.id, admin_id=admin.id, reason="x")
    del fake_paystack.transfers[p.reference]
    with pytest.raises(payouts.PayoutConflict, match="no record"):
        payouts.retry_payout(db, p.id, admin_id=admin.id, reason="x")
    fake_paystack.offline = True
    with pytest.raises(payouts.PayoutConflict, match="cannot reach Paystack"):
        payouts.retry_payout(db, p.id, admin_id=admin.id, reason="x")
    q = payouts_of(db, cg)[0]
    assert q.status == "failed" and q.attempts == 1 and wallet_balance(db, cg) == 1_000_000
    assert len(attempts_of(db, p.id)) == 1


def test_retry_when_paystack_says_it_succeeded_settles_it_as_needs_attention_instead(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    fake_paystack.transfers[p.reference]["status"] = "success"  # we missed the webhook
    with pytest.raises(payouts.PayoutConflict, match="successful"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and wallet_balance(db, cg) == 0


def test_a_transfer_paystack_refused_can_be_retried_though_it_has_no_record(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.balance_kobo = 0
    payouts.dispatch_due(db)
    q = payouts_of(db, cg)[0]
    assert q.status == "failed" and attempts_of(db, p.id)[0].status == "refused"
    fake_paystack.balance_kobo = 10 ** 12
    payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="topped up")
    assert payouts_of(db, cg)[0].status == "processing" and wallet_balance(db, cg) == 0
    assert attempts_of(db, p.id)[0].failure_reason  # the refusal stays on the attempt


# --- 1b. a late success while a newer live payout holds the caregiver's money ------------------------------------------

NEXT_MONDAY = MONDAY + timedelta(days=7)


def failed_then_next_batch(db, client, fake, *, approve_next=False, dispatch_next=False):
    """P1 was sent and failed (its money came back), then the next batch reserved the caregiver's balance as P2."""
    cg, p1 = sent_then(db, client, fake, "failed")
    assert wallet_balance(db, cg) == 1_000_000
    assert make_batch(db, NEXT_MONDAY) is not None
    if approve_next:
        approve(db, db.query(PayoutBatch).filter_by(period_key=NEXT_MONDAY.date().isoformat()).one(), now=NEXT_MONDAY)
    if dispatch_next:
        assert payouts.dispatch_due(db) == 1
    db.expire_all()
    p2 = db.query(Payout).filter(Payout.caregiver_id == cg.id, Payout.id != p1.id).one()
    return cg, p1, p2


@pytest.mark.parametrize("approve_next", [False, True], ids=["p2_pending_approval", "p2_processing_not_sent"])
def test_a_late_success_cancels_a_newer_payout_that_was_not_sent_and_takes_the_money_from_it(db, client, fake_paystack, approve_next):
    cg, p1, p2 = failed_then_next_batch(db, client, fake_paystack, approve_next=approve_next)
    assert p2.status == ("processing" if approve_next else "pending_approval") and wallet_balance(db, cg) == 0
    assert webhook(client, fake_paystack, p1.reference, "success").status_code == 200
    db.expire_all()
    q1, q2 = db.get(Payout, p1.id), db.get(Payout, p2.id)
    assert q1.status == "needs_attention" and q1.shortfall_kobo == 0 and frozen(db, cg)
    assert_never_over(db, cg, 1_000_000)
    assert q2.status == "cancelled" and q2.cancelled_by is None and "superseded: earlier payout" in q2.cancel_reason
    assert db.get(CaregiverTransaction, p2.transaction_id).status == "cancelled"
    assert wallet_balance(db, cg) == 0, "P2's reservation paid the take-back: the money was neither lost nor created"
    assert payouts.dispatch_due(db) == 0, "the cancelled newer payout must not be sent"
    assert fake_paystack.transfers.keys() == {p1.reference}
    assert webhook(client, fake_paystack, p1.reference, "success").status_code == 200  # a repeat settles nothing more
    assert wallet_balance(db, cg) == 0


def test_retry_that_finds_the_old_transfer_paid_cancels_a_newer_unsent_payout(db, client, fake_paystack):
    cg, p1, p2 = failed_then_next_batch(db, client, fake_paystack)
    fake_paystack.transfers[p1.reference]["status"] = "success"  # we missed the webhook
    with pytest.raises(payouts.PayoutConflict, match="successful"):
        payouts.retry_payout(db, p1.id, admin_id=make_admin(db, password=None).id, reason="x")
    db.expire_all()
    assert db.get(Payout, p1.id).status == "needs_attention" and db.get(Payout, p2.id).status == "cancelled"
    assert wallet_balance(db, cg) == 0


# --- 3. reconcile never releases money on silence ----------------------------------------------------------------------


def test_a_transfer_paystack_has_no_record_of_keeps_the_money_reserved_and_needs_attention(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.offline = True
    payouts.dispatch_due(db, now=MONDAY)
    fake_paystack.offline = False
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=20)) == 0  # early: nothing
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=31)) == 1
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and q.failure_reason == "Paystack has no record; check before releasing"
    assert wallet_balance(db, cg) == 0 and ledger(db, cg)[0].status == "pending"
    assert frozen(db, cg) and assert_never_over(db, cg, 1_000_000)["reserved"] == 1_000_000
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=60)) == 0  # not asked again


def test_a_transfer_that_turns_up_after_the_flag_settles_cleanly(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.offline = True
    payouts.dispatch_due(db, now=MONDAY)
    fake_paystack.offline = False
    payouts.reconcile(db, now=MONDAY + timedelta(minutes=31))
    fake_paystack.transfers[p.reference] = {"reference": p.reference, "amount": 1_000_000, "recipient": p.recipient_code,
                                            "status": "pending", "transfer_code": "TRF_late"}
    webhook(client, fake_paystack, p.reference, "success")
    q = payouts_of(db, cg)[0]
    assert q.status == "success" and wallet_balance(db, cg) == 0 and ledger(db, cg)[0].status == "successful"


# --- 4. the send job re-checks eligibility -----------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["suspended", "under_review", "inactive", "erased"])
def test_the_send_job_cancels_a_payout_whose_caregiver_is_no_longer_payable(db, fake_paystack, change):
    cg, p = approved_payout(db, fake_paystack)
    if change == "inactive":
        cg.user.is_active = False
    elif change == "erased":
        cg.suspension_reason = "account_erased"
        cg.user.is_active = False
    else:
        cg.profile_status = change
    db.commit()
    assert payouts.dispatch_due(db) == 0
    q = payouts_of(db, cg)[0]
    assert q.status == "cancelled" and q.cancelled_by is None and q.cancelled_at is not None
    assert "send" in q.cancel_reason
    assert wallet_balance(db, cg) == 1_000_000 and ledger(db, cg)[0].status == "cancelled"
    assert not fake_paystack.transfers, "nothing may be sent"


def test_the_send_job_still_sends_an_active_caregiver(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    cg.profile_status = "active"
    db.commit()
    assert payouts.dispatch_due(db) == 1 and len(fake_paystack.transfers) == 1


# --- 5. admin resolve --------------------------------------------------------------------------------------------------


def resolve(db, p, outcome, reason="Checked in the Paystack dashboard", force=False, admin=None):
    admin = admin or make_admin(db, password=None)
    return payouts.resolve_payout(db, p.id, outcome=outcome, reason=reason, force=force, admin_id=admin.id)


def stuck_at_otp(db, fake):
    fake.transfer_initial_status = "otp"
    cg, p = approved_payout(db, fake)
    payouts.dispatch_due(db)
    return cg, payouts_of(db, cg)[0]


def test_resolve_paid_for_a_transfer_stuck_at_otp_once_paystack_confirms_it(db, fake_paystack):
    cg, p = stuck_at_otp(db, fake_paystack)
    fake_paystack.transfers[p.reference]["status"] = "success"
    resolve(db, p, "paid")
    q = payouts_of(db, cg)[0]
    assert q.status == "success" and ledger(db, cg)[0].status == "successful" and wallet_balance(db, cg) == 0
    assert attempts_of(db, p.id)[0].status == "success"
    row = db.query(AdminAuditLog).filter_by(action="payout.resolve", entity_id=str(p.id)).one()
    assert row.reason == "Checked in the Paystack dashboard" and row.after["status"] == "success"


def test_resolve_failed_returns_the_reserved_money_once_paystack_confirms_the_failure(db, fake_paystack):
    cg, p = stuck_at_otp(db, fake_paystack)
    fake_paystack.transfers[p.reference]["status"] = "failed"
    resolve(db, p, "failed")
    q = payouts_of(db, cg)[0]
    assert q.status == "failed" and ledger(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 1_000_000
    with pytest.raises(payouts.PayoutConflict):
        resolve(db, p, "failed")  # already resolved: the money is not returned twice
    assert wallet_balance(db, cg) == 1_000_000


@pytest.mark.parametrize("paystack_says, outcome", [("success", "failed"), ("failed", "paid"), ("reversed", "paid")])
def test_resolve_is_refused_when_paystack_contradicts_it_even_with_force(db, fake_paystack, paystack_says, outcome):
    cg, p = stuck_at_otp(db, fake_paystack)
    fake_paystack.transfers[p.reference]["status"] = paystack_says
    with pytest.raises(payouts.PayoutConflict, match="Paystack"):
        resolve(db, p, outcome, force=True)
    assert payouts_of(db, cg)[0].status == "processing" and wallet_balance(db, cg) == 0


@pytest.mark.parametrize("outcome", ["paid", "failed"])
def test_resolve_with_an_unknown_answer_needs_force(db, fake_paystack, outcome):
    cg, p = stuck_at_otp(db, fake_paystack)  # Paystack says "otp": not a final answer
    with pytest.raises(payouts.PayoutConflict, match="force"):
        resolve(db, p, outcome)
    assert payouts_of(db, cg)[0].status == "processing"
    resolve(db, p, outcome, force=True)
    q = payouts_of(db, cg)[0]
    assert q.status == ("success" if outcome == "paid" else "failed")
    assert wallet_balance(db, cg) == (0 if outcome == "paid" else 1_000_000)
    row = db.query(AdminAuditLog).filter_by(action="payout.resolve").one()
    assert row.after["forced"] is True


def test_resolve_failed_after_the_no_record_flag_releases_the_money(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.offline = True
    payouts.dispatch_due(db, now=MONDAY)
    fake_paystack.offline = False
    payouts.reconcile(db, now=MONDAY + timedelta(minutes=31))
    with pytest.raises(payouts.PayoutConflict, match="force"):
        resolve(db, p, "failed")  # Paystack still has no record: that is unknown, not a failure
    resolve(db, p, "failed", force=True)
    assert payouts_of(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 1_000_000


def test_resolve_of_a_paid_twice_payout_can_only_confirm_it_was_paid(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    with pytest.raises(payouts.PayoutConflict, match="paid"):
        resolve(db, p, "failed", force=True)  # would give the money back a second time
    set_balance(db, 250_000)
    resolve(db, p, "paid")
    q = payouts_of(db, cg)[0]
    assert q.status == "success" and wallet_balance(db, cg) == 250_000


@pytest.mark.parametrize("make_state", ["pending_approval", "success", "failed", "cancelled"])
def test_resolve_only_works_for_processing_or_needs_attention(db, client, fake_paystack, make_state):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    (p,) = payouts_of(db, cg)
    admin = make_admin(db, password=None)
    if make_state == "cancelled":
        payouts.cancel_payout(db, p.id, admin_id=admin.id, reason="x")
    elif make_state != "pending_approval":
        approve(db, batch)
        payouts.dispatch_due(db)
        webhook(client, fake_paystack, p.reference, make_state)
    with pytest.raises(payouts.PayoutConflict, match="cannot be resolved"):
        resolve(db, p, "paid", admin=admin)


def test_resolve_through_the_api(db, client, fake_paystack):
    cg, p = stuck_at_otp(db, fake_paystack)
    boss, finance = make_admin(db, BOSS, password=None), make_admin(db, ["finance"], password=None)
    url = f"{ADMIN}/{p.id}/resolve"
    assert client.post(url, json={"outcome": "paid", "reason": "x"}, headers=auth_headers(finance)).status_code == 403
    assert client.post(url, json={"outcome": "maybe", "reason": "x"}, headers=auth_headers(boss)).status_code == 422
    assert client.post(url, json={"outcome": "paid", "reason": " "}, headers=auth_headers(boss)).status_code == 422
    r = client.post(url, json={"outcome": "paid", "reason": "Confirmed"}, headers=auth_headers(boss))
    assert r.status_code == 409 and "force" in r.json()["detail"]
    r = client.post(url, json={"outcome": "paid", "reason": "Confirmed with Paystack support", "force": True},
                    headers=auth_headers(boss))
    assert r.status_code == 200 and r.json()["status"] == "success"
    missing = f"{ADMIN}/00000000-0000-0000-0000-000000000000/resolve"
    assert client.post(missing, json={"outcome": "paid", "reason": "x"}, headers=auth_headers(boss)).status_code == 404


def test_the_payout_list_and_detail_carry_the_new_status_and_shortfall(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    boss = make_admin(db, BOSS, password=None)
    body = client.get(f"{ADMIN}?status=needs_attention", headers=auth_headers(boss)).json()
    assert body["total"] == 1 and body["items"][0]["status"] == "needs_attention" and body["items"][0]["shortfall_kobo"] == 0
    assert [a["attempt"] for a in body["items"][0]["attempt_history"]] == [1]
    assert body["items"][0]["attempt_history"][0]["status"] == "success"


# --- 5b. cancelling a whole unapproved batch ---------------------------------------------------------------------------


def test_cancelling_a_batch_returns_every_reserved_amount_and_audits(db, fake_paystack):
    a, b = payable(db, 1_000_000, fake=fake_paystack), payable(db, 2_000_000, fake=fake_paystack)
    batch = make_batch(db)
    admin = make_admin(db, password=None)
    done = payouts.cancel_batch(db, batch.id, admin_id=admin.id, reason="Totals do not match Finance")
    assert done.status == "cancelled"
    assert wallet_balance(db, a) == 1_000_000 and wallet_balance(db, b) == 2_000_000
    for cg in (a, b):
        (p,) = payouts_of(db, cg)
        assert p.status == "cancelled" and p.cancelled_by == admin.id and ledger(db, cg)[0].status == "cancelled"
    row = db.query(AdminAuditLog).filter_by(action="payout_batch.cancel", entity_id=str(batch.id)).one()
    assert row.reason == "Totals do not match Finance" and row.after["returned_total_kobo"] == 3_000_000
    # the money is free to be reserved by the next batch
    assert payouts.create_batch(db, period_key="2026-10-19", now=MONDAY + timedelta(days=7)) is not None


def test_a_batch_that_was_approved_or_cancelled_cannot_be_cancelled(db, fake_paystack):
    payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    admin = make_admin(db, password=None)
    approve(db, batch)
    with pytest.raises(payouts.PayoutConflict, match="already approved"):
        payouts.cancel_batch(db, batch.id, admin_id=admin.id, reason="x")
    payable(db, 1_000_000, fake=fake_paystack)
    other = payouts.create_batch(db, period_key="2026-10-19", now=MONDAY + timedelta(days=7))
    payouts.cancel_batch(db, other.id, admin_id=admin.id, reason="x")
    with pytest.raises(payouts.PayoutConflict, match="already cancelled"):
        payouts.cancel_batch(db, other.id, admin_id=admin.id, reason="x")
    with pytest.raises(payouts.PayoutNotFound):
        payouts.cancel_batch(db, __import__("uuid").uuid4(), admin_id=admin.id, reason="x")


def test_cancelling_a_batch_through_the_api(db, client, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    boss, support = make_admin(db, BOSS, password=None), make_admin(db, ["support"], password=None)
    url = f"{ADMIN}/batches/{batch.id}/cancel"
    assert client.post(url, json={"reason": "x"}, headers=auth_headers(support)).status_code == 403  # Finance may cancel (payouts.execute)
    assert client.post(url, json={}, headers=auth_headers(boss)).status_code == 422
    r = client.post(url, json={"reason": "Wrong week"}, headers=auth_headers(boss))
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["counts"] == {"cancelled": 1}
    assert wallet_balance(db, cg) == 1_000_000
    assert client.post(url, json={"reason": "again"}, headers=auth_headers(boss)).status_code == 409
    missing = client.post(f"{ADMIN}/batches/00000000-0000-0000-0000-000000000000/cancel", json={"reason": "x"},
                          headers=auth_headers(boss))
    assert missing.status_code == 404 and missing.json()["detail"] == "Payout batch not found"


# --- 6. first bank account with money in the wallet --------------------------------------------------------------------

ACCOUNT = "/api/v1/caregiver/bank-account"


def test_the_first_bank_account_starts_the_cool_down_when_there_is_money_in_the_wallet(db, client, fake_paystack):
    from app.db.models.payout import CaregiverBankEvent
    from app.tests.factories import make_caregiver
    cg = make_caregiver(db)
    db.add(CaregiverWallet(caregiver_id=cg.id, balance=500_000))
    db.commit()
    before = datetime.utcnow()
    r = client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 200
    blocked = datetime.fromisoformat(r.json()["payouts_blocked_until"])
    assert timedelta(hours=23, minutes=59) < blocked - before < timedelta(hours=24, minutes=1)
    db.expire_all()
    (event,) = db.query(CaregiverBankEvent).all()
    assert event.payout_blocked_until == db.query(CaregiverBankAccount).one().payout_blocked_until is not None
    assert event.action == "bank_account.set"


def test_the_first_bank_account_with_an_empty_wallet_has_no_cool_down(db, client, fake_paystack):
    from app.db.models.payout import CaregiverBankEvent
    from app.tests.factories import make_caregiver
    cg = make_caregiver(db)
    r = client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 200 and r.json()["payouts_blocked_until"] is None
    assert db.query(CaregiverBankEvent).one().payout_blocked_until is None


# --- 7. the worker comment ---------------------------------------------------------------------------------------------


def test_the_worker_comment_names_the_real_batch_time():
    import inspect
    from app import worker
    line = next(l for l in inspect.getsource(worker).splitlines() if "cron(payout_batch_job" in l)
    assert "hour=6, minute=0, second=10" in line and "06:00:10" in line


# --- review follow-ups --------------------------------------------------------------------------------------------------


def test_a_success_for_a_different_amount_is_flagged_and_never_sent_again(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    assert payouts.apply_transfer_event(db, p.reference, "success", amount_kobo=999) == "amount_mismatch"
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and "999" in q.failure_reason and wallet_balance(db, cg) == 0
    assert frozen(db, cg) and "different amount" in frozen_reason(db, cg)
    assert_never_over(db, cg, 1_000_000)
    assert ledger(db, cg)[0].status == "pending", "the money stays reserved until a person looks"
    assert payouts.dispatch_due(db) == 0
    assert payouts.apply_transfer_event(db, p.reference, "success", amount_kobo=999) == "amount_mismatch"  # idempotent
    assert payouts_of(db, cg)[0].status == "needs_attention"


@pytest.mark.parametrize("http_status", [408, 409])
def test_a_408_or_409_from_paystack_leaves_the_payout_processing(db, fake_paystack, http_status):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.transfer_error = (http_status, "unclear")
    payouts.dispatch_due(db)
    q = payouts_of(db, cg)[0]
    assert q.status == "processing" and wallet_balance(db, cg) == 0
    assert attempts_of(db, p.id)[0].status == "pending"


@pytest.mark.parametrize("http_status", [400, 401, 404, 422])
def test_other_4xx_answers_from_paystack_still_refuse_the_transfer(db, fake_paystack, http_status):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.transfer_error = (http_status, "no")
    payouts.dispatch_due(db)
    assert payouts_of(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 1_000_000


# --- 8. the invariant: once a caregiver's payouts are disputed, automatic code only lowers what they can be paid -----------
#
# A contradiction (Paystack's word disagrees with what we recorded) freezes the caregiver. From then on no automatic step
# credits the balance; money returns only through resolve / unfreeze. Every test below ends with assert_never_over: what
# Paystack paid (less a recorded shortfall) + the available balance + what is reserved never exceeds what was earned.

EARNED = 1_000_000


def p1_paid_late_while_p2_was_sent(db, client, fake):
    """P1 failed (money back), the next batch reserved it as P2 and P2 was sent, then Paystack says P1 succeeded after all."""
    cg, p1, p2 = failed_then_next_batch(db, client, fake, approve_next=True, dispatch_next=True)
    assert webhook(client, fake, p1.reference, "success").status_code == 200
    return cg, p1, p2


def test_late_p1_success_with_p2_sent_flags_both_and_records_the_shortfall_without_crediting(db, client, fake_paystack):
    cg, p1, p2 = p1_paid_late_while_p2_was_sent(db, client, fake_paystack)
    q1, q2 = payouts_of(db, cg, id=p1.id)[0], payouts_of(db, cg, id=p2.id)[0]
    assert q1.status == q2.status == "needs_attention" and q1.shortfall_kobo == 1_000_000 and q2.shortfall_kobo == 0
    assert [t.status for t in ledger(db, cg)] == ["successful", "pending"]
    assert wallet_balance(db, cg) == 0 and frozen(db, cg)
    assert_never_over(db, cg, EARNED)


def test_late_p1_success_then_p2_success_settles_p2_and_moves_no_money(db, client, fake_paystack):
    cg, p1, p2 = p1_paid_late_while_p2_was_sent(db, client, fake_paystack)
    assert webhook(client, fake_paystack, p2.reference, "success").status_code == 200
    q1, q2 = payouts_of(db, cg, id=p1.id)[0], payouts_of(db, cg, id=p2.id)[0]
    assert (q1.status, q2.status) == ("needs_attention", "success")
    assert wallet_balance(db, cg) == 0 and [t.status for t in ledger(db, cg)] == ["successful", "successful"]
    state = assert_never_over(db, cg, EARNED)
    assert state["paid"] == 2_000_000 and state["shortfall"] == 1_000_000, "paid twice, and it is on record"
    assert payouts.create_batch(db, period_key="2026-10-26", now=MONDAY + timedelta(days=14)) is None


@pytest.mark.parametrize("outcome", ["failed", "reversed"])
def test_late_p1_success_then_p2_fails_keeps_the_reservation_and_an_admin_nets_it_off(db, client, fake_paystack, outcome):
    cg, p1, p2 = p1_paid_late_while_p2_was_sent(db, client, fake_paystack)
    assert webhook(client, fake_paystack, p2.reference, outcome).status_code == 200
    q2 = payouts_of(db, cg, id=p2.id)[0]
    assert q2.status == "needs_attention" and wallet_balance(db, cg) == 0, "no money returns while the caregiver is frozen"
    assert ledger(db, cg)[1].status == "pending" and attempts_of(db, p2.id)[0].status == outcome
    assert assert_never_over(db, cg, EARNED)["reserved"] == 1_000_000
    admin = make_admin(db, password=None)
    resolve(db, p2, "failed", admin=admin)
    q1, q2 = payouts_of(db, cg, id=p1.id)[0], payouts_of(db, cg, id=p2.id)[0]
    assert q2.status == "failed" and (q2.returned_kobo, q2.netted_kobo) == (0, 1_000_000)
    assert q1.shortfall_kobo == 0 and wallet_balance(db, cg) == 0, "P2's money was kept against what P1 had already paid"
    tx2 = ledger(db, cg)[1]
    assert tx2.status == "failed" and "0 kobo returned" in tx2.status_reason and "1000000 kobo kept" in tx2.status_reason
    assert tx2.status_changed_by == admin.id
    row = db.query(AdminAuditLog).filter_by(action="payout.resolve", entity_id=str(p2.id)).one()
    assert row.after["returned_kobo"] == 0 and row.after["netted_kobo"] == 1_000_000
    assert assert_never_over(db, cg, EARNED)["reserved"] == 0
    resolve(db, p1, "paid", admin=admin)  # P1 was paid: that closes the last open payout
    payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Both transfers accounted for")
    assert not frozen(db, cg) and payouts_of(db, cg, id=p1.id)[0].status == "success"


def test_resolve_failed_returns_only_the_part_of_the_reservation_above_the_shortfall(db, client, fake_paystack):
    cg, p1 = sent_then(db, client, fake_paystack, "failed")
    set_balance(db, 1_400_000)  # 400000 of earnings arrived before the next batch
    make_batch(db, NEXT_MONDAY)
    approve(db, db.query(PayoutBatch).filter_by(period_key=NEXT_MONDAY.date().isoformat()).one(), now=NEXT_MONDAY)
    assert payouts.dispatch_due(db) == 1
    p2 = payouts_of(db, cg, id=payouts_of(db, cg)[-1].id)[0]
    assert p2.amount_kobo == 1_400_000
    webhook(client, fake_paystack, p1.reference, "success")  # P2 was sent, so P1's 1000000 is a shortfall
    webhook(client, fake_paystack, p2.reference, "failed")
    resolve(db, p2, "failed")
    q1, q2 = payouts_of(db, cg, id=p1.id)[0], payouts_of(db, cg, id=p2.id)[0]
    assert (q2.returned_kobo, q2.netted_kobo, q1.shortfall_kobo) == (400_000, 1_000_000, 0)
    assert wallet_balance(db, cg) == 400_000
    assert_never_over(db, cg, 1_400_000)


def test_a_shortfall_with_nothing_reserved_stays_recorded(db, client, fake_paystack):
    cg, p1 = sent_then(db, client, fake_paystack, "failed")
    set_balance(db, 300_000)
    webhook(client, fake_paystack, p1.reference, "success")  # 700000 shortfall on P1, 300000 taken back
    assert payouts_of(db, cg)[0].shortfall_kobo == 700_000 and wallet_balance(db, cg) == 0
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and ledger(db, cg)[0].status == "successful"
    # Nothing else is reserved, so nothing can be netted; the shortfall stays until an admin writes it off.
    assert_never_over(db, cg, EARNED)


def test_a_reversal_after_success_freezes_and_returns_nothing_until_an_admin_resolves_it(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "success")
    assert webhook(client, fake_paystack, p.reference, "reversed").status_code == 200
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and wallet_balance(db, cg) == 0, "a reversal must not credit the wallet by itself"
    assert ledger(db, cg)[0].status == "pending" and attempts_of(db, p.id)[0].status == "reversed" and frozen(db, cg)
    assert assert_never_over(db, cg, EARNED)["reserved"] == 1_000_000
    admin = make_admin(db, password=None)
    with pytest.raises(payouts.PayoutConflict, match="failed or reversed"):
        payouts.retry_payout(db, p.id, admin_id=admin.id, reason="x")
    resolve(db, p, "failed", admin=admin)  # Paystack agrees it was reversed
    q = payouts_of(db, cg)[0]
    assert q.status == "failed" and (q.returned_kobo, q.netted_kobo) == (1_000_000, 0) and wallet_balance(db, cg) == 1_000_000
    assert_never_over(db, cg, EARNED)
    payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Reversal confirmed and returned")
    assert payouts.create_batch(db, period_key="2026-10-19", now=MONDAY + timedelta(days=7)) is not None


def test_a_reversal_after_success_beside_a_sent_payout_freezes_and_that_payouts_failure_credits_nothing(db, client, fake_paystack):
    cg, p1 = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p1.reference, "success")
    set_balance(db, 600_000)  # earnings after the payment, reserved by the next batch as P2 and sent
    make_batch(db, NEXT_MONDAY)
    approve(db, db.query(PayoutBatch).filter_by(period_key=NEXT_MONDAY.date().isoformat()).one(), now=NEXT_MONDAY)
    assert payouts.dispatch_due(db) == 1
    p2 = payouts_of(db, cg)[-1]
    webhook(client, fake_paystack, p1.reference, "reversed")
    q1, q2 = payouts_of(db, cg, id=p1.id)[0], payouts_of(db, cg, id=p2.id)[0]
    assert (q1.status, q2.status) == ("needs_attention", "processing") and frozen(db, cg) and wallet_balance(db, cg) == 0
    assert_never_over(db, cg, 1_600_000)
    webhook(client, fake_paystack, p2.reference, "failed")  # frozen: recorded, but its money stays reserved
    q2 = payouts_of(db, cg, id=p2.id)[0]
    assert q2.status == "needs_attention" and wallet_balance(db, cg) == 0 and ledger(db, cg)[1].status == "pending"
    assert assert_never_over(db, cg, 1_600_000)["reserved"] == 1_600_000
    admin = make_admin(db, password=None)
    resolve(db, p2, "failed", admin=admin)
    resolve(db, p1, "failed", admin=admin)
    assert wallet_balance(db, cg) == 1_600_000
    assert_never_over(db, cg, 1_600_000)
    payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Both returned after checking Paystack")


def test_a_reversal_after_a_late_success_take_back_returns_only_through_resolve(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    set_balance(db, 300_000)
    webhook(client, fake_paystack, p.reference, "success")  # 300000 taken back, 700000 shortfall
    webhook(client, fake_paystack, p.reference, "reversed")
    assert wallet_balance(db, cg) == 0 and payouts_of(db, cg)[0].status == "needs_attention"
    resolve(db, p, "failed")  # netted: the shortfall is kept out of the 1000000 reservation
    q = payouts_of(db, cg)[0]
    assert (q.returned_kobo, q.netted_kobo, q.shortfall_kobo) == (300_000, 700_000, 0) and wallet_balance(db, cg) == 300_000
    assert_never_over(db, cg, EARNED)


def test_an_amount_mismatch_after_the_money_came_back_freezes_without_moving_money(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    assert payouts.apply_transfer_event(db, p.reference, "success", amount_kobo=999) == "amount_mismatch"
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention", "a payout that is not processing is flagged too, or nothing would block the unfreeze"
    assert wallet_balance(db, cg) == 1_000_000 and ledger(db, cg)[0].status == "failed" and frozen(db, cg)
    assert_never_over(db, cg, EARNED)
    assert payouts.create_batch(db, period_key="2026-10-19", now=MONDAY + timedelta(days=7)) is None


def test_a_failure_of_a_frozen_caregivers_payout_returns_no_money(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.offline = True
    payouts.dispatch_due(db, now=MONDAY)
    fake_paystack.offline = False
    payouts.reconcile(db, now=MONDAY + timedelta(minutes=31))  # no record: frozen, needs_attention, money reserved
    assert frozen(db, cg)
    fake_paystack.transfers[p.reference] = {"reference": p.reference, "amount": 1_000_000, "recipient": p.recipient_code,
                                            "status": "pending", "transfer_code": "TRF_late"}
    webhook(client, fake_paystack, p.reference, "failed")
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and wallet_balance(db, cg) == 0 and ledger(db, cg)[0].status == "pending"
    assert "Later, Paystack reported attempt 1 as failed" in q.failure_reason
    assert assert_never_over(db, cg, EARNED)["reserved"] == 1_000_000
    resolve(db, p, "failed")
    assert wallet_balance(db, cg) == 1_000_000 and payouts_of(db, cg)[0].status == "failed"


def test_a_frozen_caregiver_is_excluded_from_the_batch_the_send_job_and_a_retry(db, client, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    other = payable(db, 1_000_000, fake=fake_paystack)
    db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"payouts_frozen_at": datetime.utcnow()})
    db.commit()
    batch = make_batch(db)
    assert payouts_of(db, cg) == [] and len(payouts_of(db, other)) == 1 and wallet_balance(db, cg) == 1_000_000
    approve(db, batch)
    assert payouts.dispatch_due(db) == 1 and set(fake_paystack.transfers) == {payouts_of(db, other)[0].reference}


def test_a_payout_of_a_caregiver_who_is_frozen_before_the_send_job_runs_is_not_sent(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"payouts_frozen_at": datetime.utcnow()})
    db.commit()
    assert payouts.dispatch_due(db) == 0 and not fake_paystack.transfers
    assert payouts_of(db, cg)[0].status == "processing" and wallet_balance(db, cg) == 0


def test_a_failed_payout_of_a_frozen_caregiver_cannot_be_retried(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"payouts_frozen_at": datetime.utcnow()})
    db.commit()
    with pytest.raises(payouts.PayoutConflict, match="frozen"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")
    assert payouts_of(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 1_000_000


def test_approving_a_batch_cancels_the_payout_of_a_caregiver_frozen_since_it_was_made(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"payouts_frozen_at": datetime.utcnow()})
    db.commit()
    approve(db, batch)
    q = payouts_of(db, cg)[0]
    assert q.status == "cancelled" and "frozen" in q.cancel_reason and wallet_balance(db, cg) == 1_000_000


# --- 8b. unfreeze --------------------------------------------------------------------------------------------------------


def test_unfreeze_is_refused_while_a_payout_needs_attention_and_allowed_once_it_is_resolved(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    admin = make_admin(db, password=None)
    with pytest.raises(payouts.PayoutConflict, match="need attention"):
        payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Looks fine")
    assert frozen(db, cg)
    resolve(db, p, "paid", admin=admin)
    out = payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Paystack confirmed the payment")
    assert not frozen(db, cg) and out["written_off_shortfall_kobo"] == 0 and out["outstanding_shortfall_kobo"] == 0
    row = db.query(AdminAuditLog).filter_by(action="payout.unfreeze", entity_id=str(cg.id)).one()
    assert row.reason == "Paystack confirmed the payment" and row.before["payouts_frozen_at"] and row.after["payouts_frozen_at"] is None
    set_balance(db, 600_000)  # paid again from here on
    assert payouts.create_batch(db, period_key="2026-10-19", now=MONDAY + timedelta(days=7)) is not None
    with pytest.raises(payouts.PayoutConflict, match="not frozen"):
        payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="again")


def test_unfreeze_is_refused_while_a_payout_is_processing(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"payouts_frozen_at": datetime.utcnow()})
    db.commit()
    with pytest.raises(payouts.PayoutConflict, match="processing"):
        payouts.unfreeze_caregiver(db, cg.id, admin_id=make_admin(db, password=None).id, reason="x")


def shortfall_of_700k(db, client, fake):
    """P was sent and failed, 300000 of earnings were in the wallet when Paystack paid it after all: 300000 taken back, 700000
    shortfall, balance 0. The payout is then resolved as paid, so only the freeze and the shortfall are left."""
    cg, p = sent_then(db, client, fake, "failed")
    set_balance(db, 300_000)
    webhook(client, fake, p.reference, "success")
    admin = make_admin(db, password=None)
    resolve(db, p, "paid", admin=admin)
    assert payouts_of(db, cg)[0].shortfall_kobo == 700_000 and wallet_balance(db, cg) == 0 and frozen(db, cg)
    return cg, p, admin


def test_unfreeze_with_a_shortfall_nothing_can_cover_is_refused_unless_it_is_written_off(db, client, fake_paystack):
    cg, p, admin = shortfall_of_700k(db, client, fake_paystack)
    with pytest.raises(payouts.PayoutConflict, match="write_off_shortfall") as refused:
        payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Reviewed")
    assert "700000" in str(refused.value)
    assert frozen(db, cg) and payouts_of(db, cg)[0].shortfall_kobo == 700_000, "a refusal changes nothing"
    assert db.query(AdminAuditLog).filter_by(action="payout.unfreeze").count() == 0
    out = payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Reviewed", write_off_shortfall=True)
    assert out == {"caregiver_id": cg.id, "payouts_frozen_at": None, "debited_from_balance_kobo": 0,
                   "written_off_shortfall_kobo": 700_000, "outstanding_shortfall_kobo": 0}
    assert not frozen(db, cg) and payouts_of(db, cg)[0].shortfall_kobo == 0
    row = db.query(AdminAuditLog).filter_by(action="payout.unfreeze").one()
    assert row.after["written_off_shortfall_kobo"] == 700_000 and row.before["outstanding_shortfall_kobo"] == 700_000


def test_unfreeze_first_debits_the_shortfall_from_earnings_that_built_up_while_frozen(db, client, fake_paystack):
    cg, p, admin = shortfall_of_700k(db, client, fake_paystack)
    set_balance(db, 1_000_000)  # earnings keep accruing while the caregiver is frozen
    out = payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Reviewed")  # no write-off needed
    assert out["debited_from_balance_kobo"] == 700_000 and out["written_off_shortfall_kobo"] == 0
    assert out["outstanding_shortfall_kobo"] == 0
    assert wallet_balance(db, cg) == 300_000 and payouts_of(db, cg)[0].shortfall_kobo == 0 and not frozen(db, cg)
    tx = ledger(db, cg)[0]
    assert "700000" in tx.status_reason and tx.status_changed_by == admin.id and tx.status == "successful"
    row = db.query(AdminAuditLog).filter_by(action="payout.unfreeze").one()
    assert row.after["debited_from_balance_kobo"] == 700_000 and row.before["outstanding_shortfall_kobo"] == 700_000
    assert_never_over(db, cg, 1_000_000 + 300_000)


@pytest.mark.parametrize("write_off", [False, True])
def test_unfreeze_debits_what_the_balance_holds_and_the_rest_needs_a_write_off(db, client, fake_paystack, write_off):
    cg, p, admin = shortfall_of_700k(db, client, fake_paystack)
    set_balance(db, 400_000)
    if not write_off:
        with pytest.raises(payouts.PayoutConflict, match="300000"):
            payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Reviewed")
        assert wallet_balance(db, cg) == 400_000 and payouts_of(db, cg)[0].shortfall_kobo == 700_000 and frozen(db, cg)
        return
    out = payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Reviewed", write_off_shortfall=True)
    assert out["debited_from_balance_kobo"] == 400_000 and out["written_off_shortfall_kobo"] == 300_000
    assert out["outstanding_shortfall_kobo"] == 0 and wallet_balance(db, cg) == 0 and payouts_of(db, cg)[0].shortfall_kobo == 0
    row = db.query(AdminAuditLog).filter_by(action="payout.unfreeze").one()
    assert row.after["debited_from_balance_kobo"] == 400_000 and row.after["written_off_shortfall_kobo"] == 300_000


def test_unfreeze_through_the_api(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    boss, finance = make_admin(db, BOSS, password=None), make_admin(db, ["finance"], password=None)
    url = f"{ADMIN}/caregivers/{cg.id}/unfreeze"
    assert client.post(url, json={"reason": "x"}, headers=auth_headers(finance)).status_code == 403
    assert client.post(url, json={"reason": " "}, headers=auth_headers(boss)).status_code == 422
    assert client.post(url, json={"reason": "x", "extra": 1}, headers=auth_headers(boss)).status_code == 422
    missing = client.post(f"{ADMIN}/caregivers/00000000-0000-0000-0000-000000000000/unfreeze", json={"reason": "x"},
                          headers=auth_headers(boss))
    assert missing.status_code == 404 and missing.json()["detail"] == "Caregiver not found"
    r = client.post(url, json={"reason": "Checked"}, headers=auth_headers(boss))
    assert r.status_code == 409 and "need attention" in r.json()["detail"]
    resolve(db, p, "paid")
    r = client.post(url, json={"reason": "Paystack confirmed", "write_off_shortfall": True}, headers=auth_headers(boss))
    assert r.status_code == 200
    assert r.json() == {"caregiver_id": str(cg.id), "payouts_frozen_at": None, "debited_from_balance_kobo": 0,
                        "written_off_shortfall_kobo": 0, "outstanding_shortfall_kobo": 0}
    assert client.post(url, json={"reason": "again"}, headers=auth_headers(boss)).status_code == 409


def test_the_payout_responses_show_the_freeze_and_the_resolution_amounts(db, client, fake_paystack):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    webhook(client, fake_paystack, p.reference, "success")
    boss = make_admin(db, BOSS, password=None)
    item = client.get(f"{ADMIN}?status=needs_attention", headers=auth_headers(boss)).json()["items"][0]
    assert item["caregiver_payouts_frozen_at"] and "Paystack paid" in item["caregiver_payouts_frozen_reason"]
    assert (item["returned_kobo"], item["netted_kobo"]) == (0, 0)
    resolve(db, p, "paid")
    payouts.unfreeze_caregiver(db, cg.id, admin_id=boss.id, reason="ok")
    item = client.get(f"{ADMIN}/batches/{p.batch_id}", headers=auth_headers(boss)).json()["payouts"][0]
    assert item["caregiver_payouts_frozen_at"] is None and item["caregiver_payouts_frozen_reason"] is None


def test_a_frozen_caregiver_cannot_delete_the_account(db, client):
    from app.tests.factories import make_caregiver
    cg = make_caregiver(db)
    db.add(CaregiverWallet(caregiver_id=cg.id, balance=0, payouts_frozen_at=datetime.utcnow()))
    db.commit()
    r = client.delete("/api/v1/auth/profile", headers=auth_headers(cg.user))
    assert r.status_code == 409 and "payouts are on hold" in r.json()["detail"]


# --- 8c. the batch race ---------------------------------------------------------------------------------------------------


def test_a_payout_reserved_while_a_late_success_waits_for_the_wallet_is_still_cancelled(db, client, fake_paystack):
    """The batch takes the wallet lock first. A late success that has locked P1 and is waiting for the wallet must look for the
    newer payout AFTER it gets the wallet, or it would miss one the batch created in between."""
    from app.services import earnings
    cg, p1 = sent_then(db, client, fake_paystack, "failed")
    reference, cg_id = p1.reference, cg.id

    def apply():
        session = SessionLocal()
        try:
            return payouts.apply_transfer_event(session, reference, "success", amount_kobo=1_000_000)
        finally:
            session.close()

    batch = PayoutBatch(period_key="2026-10-19", cadence="weekly", min_payout_kobo=500_000, status="pending_approval")
    locker = SessionLocal()
    try:
        earnings.locked_wallet(locker, cg_id)  # the batch job holds the wallet
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(apply)
            time.sleep(1.0)
            assert not future.done(), "the late success did not wait for the wallet"
            locker.add(batch)
            locker.flush()
            assert payouts._reserve(locker, batch, cg_id, 500_000, MONDAY + timedelta(days=7)) is not None
            locker.commit()  # P2 now exists, reserving the 1000000 the failure returned
            assert future.result(timeout=60) == "needs_attention"
    finally:
        locker.close()
    db.expire_all()
    q1 = db.get(Payout, p1.id)
    (q2,) = db.query(Payout).filter(Payout.caregiver_id == cg_id, Payout.id != p1.id).all()
    assert q1.status == "needs_attention" and q1.shortfall_kobo == 0
    assert q2.status == "cancelled", "the newer payout must be found and cancelled, or both would be paid"
    assert wallet_balance(db, cg) == 0 and frozen(db, cg)
    assert_never_over(db, cg, EARNED)


# --- 8e. the money review: checks that must hold under the lock, and a forced "paid" that Paystack later contradicts ----------


def retried_and_sent(db, client, fake):
    """Attempt 1 was sent and failed, the admin retried, attempt 2 was sent: the payout is processing on its second attempt."""
    cg, p = sent_then(db, client, fake, "failed")
    first = p.reference
    payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="again")
    assert payouts.dispatch_due(db) == 1
    q = payouts_of(db, cg)[0]
    assert q.attempts == 2 and q.status == "processing" and q.reference != first
    return cg, q, first, q.reference


def commit_on_another_session_while_paystack_is_asked(monkeypatch, reference, outcome, **kwargs):
    """The window ``resolve`` leaves open: no lock is held while Paystack is asked. Wrap ``verify_transfer`` so that, after it
    has answered, a webhook for ``reference`` commits on its own session (once) before the answer is returned."""
    from app.services.paystack import PaystackService
    real, calls = PaystackService.verify_transfer, []

    def verify(self, ref):
        answer = real(self, ref)
        if not calls:
            calls.append(ref)
            session = SessionLocal()
            try:
                payouts.apply_transfer_event(session, reference, outcome, **kwargs)
            finally:
                session.close()
        return answer

    monkeypatch.setattr(PaystackService, "verify_transfer", verify)
    return calls


def test_resolve_failed_is_refused_when_a_late_success_commits_while_paystack_is_asked(db, client, fake_paystack, monkeypatch):
    """The admin resolves attempt 2 as failed; Paystack says failed; before the lock is taken a late transfer.success for
    attempt 1 commits (needs_attention, frozen, the reservation stands). Crediting the reservation now would pay twice."""
    cg, p, first, second = retried_and_sent(db, client, fake_paystack)
    fake_paystack.transfers[second]["status"] = "failed"
    fake_paystack.transfers[first]["status"] = "success"
    calls = commit_on_another_session_while_paystack_is_asked(monkeypatch, first, "success", amount_kobo=1_000_000)
    with pytest.raises(payouts.PayoutConflict, match="attempt 1") as refused:
        resolve(db, p, "failed")
    assert calls == [second], "the late success must have been injected between the Paystack call and the lock"
    assert "paid" in str(refused.value)
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and (q.returned_kobo, q.netted_kobo) == (0, 0)
    assert wallet_balance(db, cg) == 0 and ledger(db, cg)[0].status == "pending", "nothing was credited"
    assert [a.status for a in attempts_of(db, p.id)] == ["success", "pending"] and frozen(db, cg)
    assert db.query(AdminAuditLog).filter_by(action="payout.resolve").count() == 0
    assert_never_over(db, cg, EARNED)
    with pytest.raises(payouts.PayoutConflict, match="already paid attempt 1"):
        resolve(db, p, "failed")  # and from now on the ordinary check refuses it too
    resolve(db, p, "paid")  # the only way out is to confirm the payment that did happen
    assert payouts_of(db, cg)[0].status == "success" and wallet_balance(db, cg) == 0


def test_resolve_paid_is_refused_when_another_attempt_is_paid_while_paystack_is_asked(db, client, fake_paystack, monkeypatch):
    """The mirror case: attempt 2 is verified as paid, and attempt 1 turns out to have been paid too."""
    cg, p, first, second = retried_and_sent(db, client, fake_paystack)
    fake_paystack.transfers[second]["status"] = "success"
    fake_paystack.transfers[first]["status"] = "success"
    calls = commit_on_another_session_while_paystack_is_asked(monkeypatch, first, "success", amount_kobo=1_000_000)
    with pytest.raises(payouts.PayoutConflict, match="attempt 1"):
        resolve(db, p, "paid")
    assert calls == [second]
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and wallet_balance(db, cg) == 0 and ledger(db, cg)[0].status == "pending"
    assert [a.status for a in attempts_of(db, p.id)] == ["success", "pending"], "attempt 2 must not be marked paid as well"
    assert db.query(AdminAuditLog).filter_by(action="payout.resolve").count() == 0
    assert_never_over(db, cg, EARNED)


def test_resolve_paid_is_refused_when_the_verified_attempt_changed_while_paystack_is_asked(db, fake_paystack, monkeypatch):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.offline = True
    payouts.dispatch_due(db, now=MONDAY)
    fake_paystack.offline = False
    payouts.reconcile(db, now=MONDAY + timedelta(minutes=31))  # no record: needs_attention, frozen, the attempt is pending
    fake_paystack.transfers[p.reference] = {"reference": p.reference, "amount": 1_000_000, "recipient": p.recipient_code,
                                            "status": "pending", "transfer_code": "TRF_late"}
    commit_on_another_session_while_paystack_is_asked(monkeypatch, p.reference, "failed")
    with pytest.raises(payouts.PayoutConflict, match="changed while Paystack was being asked"):
        resolve(db, p, "paid", force=True)
    assert [a.status for a in attempts_of(db, p.id)] == ["failed"] and payouts_of(db, cg)[0].status == "needs_attention"
    assert wallet_balance(db, cg) == 0 and ledger(db, cg)[0].status == "pending"


def test_retry_refuses_a_caregiver_who_is_frozen_after_the_checks_but_before_the_wallet_lock(db, client, fake_paystack, monkeypatch):
    cg, p = sent_then(db, client, fake_paystack, "failed")
    real, calls = payouts.earnings.locked_wallet, []

    def freeze_then_lock(session, caregiver_id):
        if not calls:
            calls.append(caregiver_id)
            other = SessionLocal()  # a contradiction freezes the caregiver; the wallet is not locked yet, so this commits
            try:
                other.query(CaregiverWallet).filter_by(caregiver_id=caregiver_id).update({"payouts_frozen_at": datetime.utcnow()})
                other.commit()
            finally:
                other.close()
        return real(session, caregiver_id)

    monkeypatch.setattr(payouts.earnings, "locked_wallet", freeze_then_lock)
    with pytest.raises(payouts.PayoutConflict, match="frozen"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="again")
    assert calls, "the freeze was never injected"
    q = payouts_of(db, cg)[0]
    assert q.status == "failed" and q.attempts == 1 and wallet_balance(db, cg) == 1_000_000
    assert ledger(db, cg)[0].status == "failed" and len(attempts_of(db, p.id)) == 1


# an amount mismatch on a payout that is not processing


@pytest.mark.parametrize("how", ["failed", "reversed", "cancelled"])
def test_an_amount_mismatch_on_a_payout_that_is_not_processing_flags_it_and_moves_no_money(db, client, fake_paystack, how):
    if how == "cancelled":
        cg = payable(db, 1_000_000, fake=fake_paystack)
        make_batch(db)
        (p,) = payouts_of(db, cg)
        payouts.cancel_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")
    else:
        cg, p = sent_then(db, client, fake_paystack, how)
    ledger_before = ledger(db, cg)[0].status
    assert payouts.apply_transfer_event(db, p.reference, "success", amount_kobo=999) == "amount_mismatch"
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and "999" in q.failure_reason
    assert wallet_balance(db, cg) == 1_000_000 and ledger(db, cg)[0].status == ledger_before and frozen(db, cg)
    with pytest.raises(payouts.PayoutConflict, match="need attention"):
        payouts.unfreeze_caregiver(db, cg.id, admin_id=make_admin(db, password=None).id, reason="x")
    assert payouts.apply_transfer_event(db, p.reference, "success", amount_kobo=999) == "amount_mismatch"  # idempotent
    assert wallet_balance(db, cg) == 1_000_000


def flagged_by_a_mismatch_after_failure(db, client, fake):
    cg, p = sent_then(db, client, fake, "failed")
    assert payouts.apply_transfer_event(db, p.reference, "success", amount_kobo=999) == "amount_mismatch"
    return cg, payouts_of(db, cg)[0]


def test_a_mismatch_flagged_payout_whose_money_is_back_can_be_closed_as_failed_with_no_money_moving(db, client, fake_paystack):
    cg, p = flagged_by_a_mismatch_after_failure(db, client, fake_paystack)  # Paystack itself says "failed"
    admin = make_admin(db, password=None)
    resolve(db, p, "failed", admin=admin)
    q = payouts_of(db, cg)[0]
    assert q.status == "failed" and (q.returned_kobo, q.netted_kobo) == (0, 0) and wallet_balance(db, cg) == 1_000_000
    assert ledger(db, cg)[0].status == "failed"
    payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="Paystack says it failed")
    assert_never_over(db, cg, EARNED)


@pytest.mark.parametrize("balance, taken, shortfall", [(1_000_000, 1_000_000, 0), (300_000, 300_000, 700_000)])
def test_resolving_a_mismatch_flagged_payout_as_paid_takes_back_the_money_that_had_been_returned(
        db, client, fake_paystack, balance, taken, shortfall):
    cg, p = flagged_by_a_mismatch_after_failure(db, client, fake_paystack)
    set_balance(db, balance)
    fake_paystack.transfers[p.reference]["status"] = "success"  # the checked answer: it was paid
    resolve(db, p, "paid")
    q = payouts_of(db, cg)[0]
    assert q.status == "success" and ledger(db, cg)[0].status == "successful"
    assert wallet_balance(db, cg) == balance - taken and q.shortfall_kobo == shortfall
    state = money_state(db, cg)
    assert state["paid"] - state["shortfall"] + state["available"] + state["reserved"] <= EARNED


# a forced "paid" that Paystack later contradicts


def forced_paid(db, fake):
    """A transfer stuck at OTP that an admin resolved as paid with force (Paystack's answer was not final)."""
    cg, p = stuck_at_otp(db, fake)
    resolve(db, p, "paid", force=True)
    q = payouts_of(db, cg)[0]
    assert q.status == "success" and attempts_of(db, p.id)[0].status == "success" and attempts_of(db, p.id)[0].forced
    assert not frozen(db, cg)
    return cg, q


@pytest.mark.parametrize("outcome", ["failed", "reversed"])
def test_a_failure_after_an_admin_forced_paid_needs_attention_freezes_and_credits_nothing(db, client, fake_paystack, outcome):
    cg, p = forced_paid(db, fake_paystack)
    assert webhook(client, fake_paystack, p.reference, outcome).status_code == 200
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and frozen(db, cg) and "after it had been paid" in q.failure_reason
    assert wallet_balance(db, cg) == 0, "the failure must not credit the wallet"
    assert ledger(db, cg)[0].status == "pending" and attempts_of(db, p.id)[0].status == outcome
    assert assert_never_over(db, cg, EARNED)["reserved"] == 1_000_000
    assert payouts.apply_transfer_event(db, p.reference, outcome) == "duplicate"  # a repeat changes nothing
    assert wallet_balance(db, cg) == 0
    resolve(db, p, "failed")  # an admin decides, with Paystack's checked answer
    assert wallet_balance(db, cg) == 1_000_000


def test_a_failure_after_a_paid_that_paystack_itself_confirmed_is_still_a_duplicate(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "success")
    assert not attempts_of(db, p.id)[0].forced
    assert payouts.apply_transfer_event(db, p.reference, "failed") == "duplicate"
    assert payouts_of(db, cg)[0].status == "success" and not frozen(db, cg)


# --- 8d. every event sequence, at random ---------------------------------------------------------------------------------

ITERATIONS, MAX_STEPS = 100, 14
FAR = datetime(2030, 1, 1)


def enabled_actions(db, cg, fake):
    """The steps that can do something now (a random pick from all of them would mostly be no-ops), with weights."""
    rows = payouts_of(db, cg)
    state = money_state(db, cg)
    sent = [a.reference for p in rows for a in attempts_of(db, p.id) if a.reference in fake.transfers]
    old = [a.reference for p in rows for a in attempts_of(db, p.id) if a.attempt != p.attempts and a.reference in fake.transfers]
    by_status = {s: [p for p in rows if p.status == s] for s in ("pending_approval", "processing", "failed", "reversed",
                                                                  "needs_attention")}
    actions = ["earn"]
    if state["available"] < 500_000 and not by_status["pending_approval"] and not by_status["processing"]:
        actions += ["earn"] * 3  # so there is money for another batch
    if state["available"] >= 500_000 and not state["frozen"] and not by_status["pending_approval"] and not by_status["processing"]:
        actions += ["batch"] * 3
    if by_status["pending_approval"]:
        actions += ["approve"] * 3
    if any(p.initiated_at is None for p in by_status["processing"]) and not state["frozen"]:
        actions += ["send"] * 3
    if sent:
        actions += ["success", "success", "failed", "reversed"]
    if state["frozen"] and any(p.initiated_at is not None for p in by_status["processing"]):
        actions += ["failed", "failed", "reversed", "success"]  # the events a frozen caregiver's sent payout may still get
    if any(a.status == "success" for p in rows for a in attempts_of(db, p.id) if a.reference in fake.transfers):
        actions += ["reverse_paid"] * (4 if by_status["processing"] else 1)  # a reversal after a success
    if old:
        actions += ["late_success"] * 3
    if any(p.reference in fake.transfers for p in by_status["processing"]):
        actions += ["no_record"]
    if by_status["failed"] or by_status["reversed"]:
        actions += ["retry"] * 2
    if by_status["processing"] or by_status["needs_attention"]:
        actions += ["resolve"]
    if state["frozen"]:
        actions += ["unfreeze", "unfreeze_write_off"]
    if state["available"] > 0 and (by_status["failed"] or by_status["reversed"]):
        actions += ["drain"] * 2  # the returned money is spent elsewhere (a charge): a late success then cannot be covered
    if state["shortfall"] > 0:  # a recorded shortfall: let earnings pile up, an admin settle the payouts, then unfreeze
        actions += ["earn", "resolve", "unfreeze", "unfreeze", "unfreeze_write_off", "unfreeze_write_off"]
    return actions


def test_no_event_sequence_pays_more_than_was_earned_or_raises_what_can_be_paid(db, fake_paystack):
    """Seeded random sequences of batch, approve, send, success / failed / reversed (for any attempt, including old ones), a
    reconcile that finds no record, retry, admin resolve and unfreeze, over up to three payouts of one caregiver. After EVERY
    step: paid (less recorded shortfall) + available + reserved <= earned; a needs_attention payout means the caregiver is
    frozen; and no step except new earnings or an admin returning money raises what the caregiver can still be paid."""
    admin = make_admin(db, password=None)
    stats = {"frozen_runs": 0, "events": 0, "frozen_processing_events": 0, "contradictions": 0, "unfreezes": 0, "write_offs": 0,
             "debits": 0, "unfreezes_with_shortfall": 0, "refused_unfreezes": 0}
    for iteration in range(ITERATIONS):
        rng = random.Random(1000 + iteration)
        cg = payable(db, 1_000_000, fake=fake_paystack)
        earned, written_off = 1_000_000, 0
        history = []
        for step in range(MAX_STEPS):
            before = money_state(db, cg)
            action = rng.choice(enabled_actions(db, cg, fake_paystack))
            history.append(action)
            admin_returns_money = False
            try:
                if action == "earn":
                    amount = rng.choice([300_000, 600_000])
                    db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"balance": CaregiverWallet.balance + amount})
                    db.commit()
                    earned += amount
                elif action == "drain":
                    keep = rng.choice([0, before["available"] // 3])
                    db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"balance": keep})
                    db.commit()
                elif action == "batch":
                    payouts.create_batch(db, period_key=f"r{iteration}-{step}", now=MONDAY)
                elif action == "approve":
                    waiting = db.query(Payout).filter_by(caregiver_id=cg.id, status="pending_approval").first()
                    if waiting is not None:
                        payouts.approve_batch(db, waiting.batch_id, admin_id=admin.id, reason="r", now=MONDAY)
                elif action == "send":
                    payouts.dispatch_due(db)
                elif action in ("success", "failed", "reversed", "late_success", "reverse_paid"):
                    refs = sorted(a.reference for p in payouts_of(db, cg) for a in attempts_of(db, p.id)
                                  if a.reference in fake_paystack.transfers)
                    if action == "late_success":
                        old = [a.reference for p in payouts_of(db, cg) for a in attempts_of(db, p.id)
                               if a.attempt != p.attempts and a.reference in fake_paystack.transfers]
                        refs = sorted(old)
                    if action == "reverse_paid":
                        refs = sorted(a.reference for p in payouts_of(db, cg) for a in attempts_of(db, p.id)
                                      if a.status == "success" and a.reference in fake_paystack.transfers)
                    if refs:
                        reference = rng.choice(refs)
                        outcome = {"late_success": "success", "reverse_paid": "reversed"}.get(action, action)
                        body = fake_paystack.settle_transfer(reference, outcome)
                        owner = db.query(Payout).join(PayoutAttempt, PayoutAttempt.payout_id == Payout.id).filter(
                            PayoutAttempt.reference == reference).one()
                        stats["frozen_processing_events"] += int(before["frozen"] and owner.status == "processing")
                        payouts.apply_transfer_event(db, reference, outcome, amount_kobo=body["data"]["amount"], now=MONDAY)
                        stats["events"] += 1
                elif action == "no_record":
                    sent = [p for p in payouts_of(db, cg, status="processing") if p.reference in fake_paystack.transfers]
                    if sent:
                        record = fake_paystack.transfers.pop(rng.choice(sent).reference)
                        payouts.reconcile(db, now=FAR)
                        fake_paystack.transfers[record["reference"]] = record  # Paystack may know it after all
                elif action == "retry":
                    done = [p for p in payouts_of(db, cg) if p.status in ("failed", "reversed")]
                    if done:
                        payouts.retry_payout(db, rng.choice(done).id, admin_id=admin.id, reason="r", now=MONDAY)
                elif action == "resolve":
                    open_ones = [p for p in payouts_of(db, cg) if p.status in ("processing", "needs_attention")]
                    if open_ones:
                        target = rng.choice(open_ones)
                        outcome = rng.choice(["paid", "failed"])
                        admin_returns_money = True
                        if target.reference not in fake_paystack.transfers:
                            fake_paystack.transfers[target.reference] = {
                                "reference": target.reference, "amount": target.amount_kobo, "recipient": target.recipient_code,
                                "status": "pending", "transfer_code": "TRF_x"}
                        payouts.resolve_payout(db, target.id, outcome=outcome, reason="r", force=rng.random() < 0.5,
                                               admin_id=admin.id, now=MONDAY)
                elif action in ("unfreeze", "unfreeze_write_off"):
                    out = payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="r",
                                                     write_off_shortfall=action == "unfreeze_write_off")
                    written_off += out["written_off_shortfall_kobo"]
                    stats["unfreezes"] += 1
                    stats["unfreezes_with_shortfall"] += int(before["shortfall"] > 0)
                    stats["write_offs"] += int(out["written_off_shortfall_kobo"] > 0)
                    stats["debits"] += int(out["debited_from_balance_kobo"] > 0)
            except (payouts.PayoutConflict, payouts.PayoutNotFound) as refused:
                db.rollback()  # an admin action refused, as it should be
                stats["refused_unfreezes"] += int(action.startswith("unfreeze") and "write_off_shortfall" in str(refused))
            # Paystack's own account of what it paid, with the shortfall ADDED to what may exist (never subtracted from what was
            # paid): an overpayment is only allowed when it is on record, outstanding or written off by an admin.
            state = assert_overpayment_on_record(db, fake_paystack, cg, earned, written_off)
            trail = f"iteration {iteration}, step {step}: {history}"
            open_attention = any(p.status == "needs_attention" for p in payouts_of(db, cg))
            assert not open_attention or state["frozen"], f"a payout needs attention but the caregiver is not frozen. {trail}"
            if action not in ("earn", "resolve") and not admin_returns_money:
                assert state["payable"] <= before["payable"], f"what can be paid went up ({before} -> {state}). {trail}"
            if state["shortfall"] > before["shortfall"]:
                assert state["available"] == 0, f"a shortfall was recorded while the balance could still cover it. {trail}"
            if before["frozen"] and action not in ("earn", "resolve"):
                # Frozen: the balance may only grow by the reservation of an unsent payout that was cancelled (money-neutral).
                grown = state["available"] - before["available"]
                assert grown <= cancelled_unsent_kobo(db, cg, before), f"a frozen caregiver was credited ({before} -> {state}). {trail}"
            if state["frozen"]:
                stats["frozen_runs"] += 1
            stats["contradictions"] += int(state["frozen"] and not before["frozen"])
        # Epilogue: an admin settles whatever is open with Paystack's own state, earnings pile up while frozen, then unfreezes
        # (with or without a write-off). The random steps rarely get this far, and this is where the shortfall is settled.
        if money_state(db, cg)["frozen"]:
            for target in [p for p in payouts_of(db, cg) if p.status in ("processing", "needs_attention")]:
                record = fake_paystack.transfers.setdefault(target.reference, {
                    "reference": target.reference, "amount": target.amount_kobo, "recipient": target.recipient_code,
                    "status": "pending", "transfer_code": "TRF_x"})
                try:
                    payouts.resolve_payout(db, target.id, outcome="paid" if record["status"] == "success" else "failed",
                                           reason="r", force=True, admin_id=admin.id, now=MONDAY)
                except payouts.PayoutConflict:
                    db.rollback()
                assert_overpayment_on_record(db, fake_paystack, cg, earned, written_off)
            if rng.random() < 0.7:
                amount = rng.choice([300_000, 600_000])
                db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).update({"balance": CaregiverWallet.balance + amount})
                db.commit()
                earned += amount
            before = money_state(db, cg)
            try:
                out = payouts.unfreeze_caregiver(db, cg.id, admin_id=admin.id, reason="r", write_off_shortfall=rng.random() < 0.5)
                written_off += out["written_off_shortfall_kobo"]
                stats["unfreezes"] += 1
                stats["unfreezes_with_shortfall"] += int(before["shortfall"] > 0)
                stats["write_offs"] += int(out["written_off_shortfall_kobo"] > 0)
                stats["debits"] += int(out["debited_from_balance_kobo"] > 0)
                assert out["outstanding_shortfall_kobo"] == 0 and not money_state(db, cg)["frozen"]
            except payouts.PayoutConflict as refused:
                db.rollback()
                stats["refused_unfreezes"] += int("write_off_shortfall" in str(refused))
                assert money_state(db, cg)["shortfall"] == before["shortfall"], "a refused unfreeze must change nothing"
            assert_overpayment_on_record(db, fake_paystack, cg, earned, written_off)
        cg.profile_status = "suspended"  # out of later iterations' batches and send runs
        db.commit()
    print("property test coverage:", stats)
    assert stats["events"] > ITERATIONS and stats["frozen_runs"] > ITERATIONS // 2, f"the sequences exercised too little: {stats}"
    assert stats["write_offs"] and stats["debits"] and stats["refused_unfreezes"], f"unfreeze was not exercised both ways: {stats}"
