"""A3 slice 2: scheduled caregiver payouts. A batch reserves each eligible caregiver's available balance, an admin approves it,
the send job pays it through Paystack, and Paystack's word (a webhook, or the reconcile job) settles it. The invariant under
test throughout: the available balance plus everything reserved or paid out is exactly what the caregiver had."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest

from app.db.models.audit import AdminAuditLog
from app.db.models.caregiver import CaregiverBankAccount, CaregiverTransaction, CaregiverWallet
from app.db.models.payout import Payout, PayoutBatch
from app.db.session import SessionLocal
from app.services import payouts
from app.tests.factories import auth_headers, make_admin
from app.tests.payout_helpers import WEBHOOK, assert_conserved, ledger, payable, payouts_of, signed, wallet_balance
from app.tests.policy_helpers import set_policy

MONDAY = datetime(2026, 10, 12, 6, 10)  # 2026-10-12 is a Monday, ISO week 42
ADMIN = "/api/v1/admin/payouts"


def make_batch(db, now=MONDAY):
    batch = payouts.create_batch(db, period_key=now.date().isoformat(), now=now)
    return batch


def approve(db, batch, admin=None, now=MONDAY):
    admin = admin or make_admin(db, ["super_admin"], password=None)
    return payouts.approve_batch(db, batch.id, admin_id=admin.id, reason="Weekly run checked", now=now)


# --- when ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cadence, day, expected", [
    ("weekly", date(2026, 10, 12), True), ("weekly", date(2026, 10, 13), False),
    ("biweekly", date(2026, 10, 12), True),   # ISO week 42
    ("biweekly", date(2026, 10, 19), False),  # ISO week 43
    ("monthly", date(2026, 11, 1), True), ("monthly", date(2026, 10, 12), False),
])
def test_payout_days(cadence, day, expected):
    assert payouts.is_payout_day(cadence, day) is expected


def test_next_payout_date_is_strictly_after_today():
    assert payouts.next_payout_date("weekly", date(2026, 10, 12)) == date(2026, 10, 19)
    assert payouts.next_payout_date("weekly", date(2026, 10, 14)) == date(2026, 10, 19)
    assert payouts.next_payout_date("monthly", date(2026, 10, 12)) == date(2026, 11, 1)


def test_the_scheduled_job_makes_a_batch_only_on_a_payout_day(db, fake_paystack):
    payable(db, 1_000_000, fake=fake_paystack)
    assert payouts.create_scheduled_batch(db, now=datetime(2026, 10, 13, 6, 10)) is None  # a Tuesday
    assert db.query(PayoutBatch).count() == 0
    batch = payouts.create_scheduled_batch(db, now=MONDAY)
    assert batch is not None and batch.period_key == "2026-10-12" and batch.cadence == "weekly"


def test_the_cadence_comes_from_the_policy(db, fake_paystack):
    payable(db, 1_000_000, fake=fake_paystack)
    set_policy(db, payout_cadence="monthly")
    assert payouts.create_scheduled_batch(db, now=MONDAY) is None  # a Monday, but not the 1st
    assert payouts.create_scheduled_batch(db, now=datetime(2026, 11, 1, 6, 10)).cadence == "monthly"


# --- creating the batch: who, and the reservation ----------------------------------------------------------------------


def test_a_batch_reserves_each_eligible_balance_as_a_pending_approval_debit(db, fake_paystack):
    a, b = payable(db, 800_000, fake=fake_paystack), payable(db, 1_250_000, fake=fake_paystack)
    batch = make_batch(db)
    assert batch.status == "pending_approval" and batch.min_payout_kobo == 500_000
    for cg, amount in ((a, 800_000), (b, 1_250_000)):
        (p,) = payouts_of(db, cg)
        (tx,) = ledger(db, cg)
        assert (p.status, p.amount_kobo, p.batch_id) == ("pending_approval", amount, batch.id)
        assert (tx.type, tx.status, tx.amount, tx.id) == ("payout", "pending_approval", amount, p.transaction_id)
        assert tx.reference == p.reference and p.reference.startswith("po-") and 16 <= len(p.reference) <= 50
        assert p.reference == p.reference.lower()
        assert wallet_balance(db, cg) == 0  # the reservation IS the debit
        assert_conserved(db, cg, amount)


def test_the_payout_copies_the_bank_account_so_a_later_change_cannot_redirect_it(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack, number="0123456789")
    make_batch(db)
    db.query(CaregiverBankAccount).update({"account_number": "0000000001", "recipient_code": "RCP_other"})
    db.commit()
    (p,) = payouts_of(db, cg)
    assert p.account_number == "0123456789" and p.recipient_code != "RCP_other" and p.bank_code == "058"


def test_below_the_minimum_waits_and_the_minimum_comes_from_the_policy(db, fake_paystack):
    cg = payable(db, 499_999, fake=fake_paystack)
    assert make_batch(db) is None and wallet_balance(db, cg) == 499_999 and payouts_of(db) == []
    set_policy(db, payout_min_naira=4000)
    batch = payouts.create_batch(db, period_key="x", now=MONDAY)
    assert batch.min_payout_kobo == 400_000 and wallet_balance(db, cg) == 0


def test_a_zero_minimum_still_never_makes_an_empty_payout(db, fake_paystack):
    cg = payable(db, 0, fake=fake_paystack)
    set_policy(db, payout_min_naira=0)
    assert make_batch(db) is None and payouts_of(db) == []


def test_pending_and_held_earnings_are_not_in_a_payout(db, fake_paystack):
    # only wallet.balance (released money) is taken; the pending rows live in the ledger, not the balance
    cg = payable(db, 1_000_000, fake=fake_paystack)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=3_000_000, status="pending_clearance"))
    db.commit()
    make_batch(db)
    assert payouts_of(db, cg)[0].amount_kobo == 1_000_000


def test_who_is_left_out(db, fake_paystack):
    paid = payable(db, 1_000_000, fake=fake_paystack)
    no_bank = payable(db, 1_000_000, bank=False)
    unverified = payable(db, 1_000_000)
    db.query(CaregiverBankAccount).filter_by(caregiver_id=unverified.id).update({"recipient_code": None})
    cooling = payable(db, 1_000_000, fake=fake_paystack, blocked_until=MONDAY + timedelta(hours=3))
    suspended = payable(db, 1_000_000, fake=fake_paystack, profile_status="suspended")
    erased = payable(db, 1_000_000, fake=fake_paystack)
    erased.user.is_active = False
    db.commit()
    make_batch(db)
    assert [p.caregiver_id for p in payouts_of(db)] == [paid.id]
    for cg in (no_bank, unverified, cooling, suspended, erased):
        assert wallet_balance(db, cg) == 1_000_000, "a left-out caregiver keeps the money"


def test_a_cool_down_that_has_ended_does_not_block(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack, blocked_until=MONDAY - timedelta(minutes=1))
    make_batch(db)
    assert len(payouts_of(db, cg)) == 1


def test_a_second_batch_for_the_same_period_is_a_no_op_and_money_is_reserved_once(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    assert make_batch(db) is not None
    assert make_batch(db) is None
    assert db.query(PayoutBatch).count() == 1 and len(payouts_of(db, cg)) == 1


def test_a_caregiver_with_a_live_payout_is_not_reserved_again(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    make_batch(db)
    db.query(CaregiverWallet).update({"balance": 900_000})  # more earnings were released since
    db.commit()
    assert payouts.create_batch(db, period_key="next-week", now=MONDAY + timedelta(days=7)) is None
    assert len(payouts_of(db, cg)) == 1 and wallet_balance(db, cg) == 900_000


def test_the_database_allows_one_live_payout_per_caregiver(db, fake_paystack):
    from sqlalchemy.exc import IntegrityError

    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    (first,) = payouts_of(db, cg)
    tx = CaregiverTransaction(caregiver_id=cg.id, type="payout", amount=1, status="pending_approval")
    db.add(tx)
    db.flush()
    db.add(Payout(batch_id=batch.id, caregiver_id=cg.id, transaction_id=tx.id, amount_kobo=1, status="processing",
                  bank_name="b", bank_code="058", account_number="0", account_name="n", recipient_code="r",
                  reference="po-second-attempt-0001", attempts=1))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_the_wallet_balance_can_never_go_negative(db, fake_paystack):
    from sqlalchemy.exc import IntegrityError

    cg = payable(db, 100, fake=fake_paystack)
    with pytest.raises(IntegrityError):
        db.query(CaregiverWallet).update({"balance": CaregiverWallet.balance - 101})
        db.commit()
    db.rollback()


def test_concurrent_batch_runs_make_one_batch_and_reserve_once(db, fake_paystack):
    cgs = [payable(db, 1_000_000, fake=fake_paystack) for _ in range(3)]
    barrier = threading.Barrier(2)

    def run():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            return payouts.create_batch(session, period_key="2026-10-12", now=MONDAY)
        finally:
            session.close()

    with ThreadPoolExecutor(2) as pool:
        results = [f.result(timeout=60) for f in [pool.submit(run), pool.submit(run)]]
    assert len([r for r in results if r is not None]) == 1
    assert db.query(PayoutBatch).count() == 1
    for cg in cgs:
        assert len(payouts_of(db, cg)) == 1
        assert_conserved(db, cg, 1_000_000)


# --- approving ---------------------------------------------------------------------------------------------------------


def test_approving_moves_payouts_to_processing_and_audits(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    admin = make_admin(db, ["super_admin"], password=None)
    payouts.approve_batch(db, batch.id, admin_id=admin.id, reason="Checked the totals against Finance", now=MONDAY)
    db.expire_all()
    (p,) = payouts_of(db, cg)
    (tx,) = ledger(db, cg)
    assert (p.status, tx.status, p.approved_by, p.approved_at) == ("processing", "pending", admin.id, MONDAY)
    b = db.get(PayoutBatch, batch.id)
    assert (b.status, b.approved_by, b.approval_reason) == ("approved", admin.id, "Checked the totals against Finance")
    row = db.query(AdminAuditLog).filter_by(action="payout_batch.approve").one()
    assert row.admin_user_id == admin.id and row.entity_id == str(batch.id) and row.reason == "Checked the totals against Finance"
    assert row.after["approved_total_kobo"] == 1_000_000 and row.after["approved_payouts"] == [str(p.id)]
    assert_conserved(db, cg, 1_000_000)
    assert fake_paystack.transfers == {}  # approving sends nothing by itself: the send job does


def test_a_batch_cannot_be_approved_twice(db, fake_paystack):
    payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    approve(db, batch)
    with pytest.raises(payouts.PayoutConflict, match="already approved"):
        approve(db, batch)


def test_approval_cancels_a_payout_whose_caregiver_is_no_longer_eligible_and_returns_the_money(db, fake_paystack):
    ok = payable(db, 1_000_000, fake=fake_paystack)
    suspended = payable(db, 700_000, fake=fake_paystack)
    changed = payable(db, 600_000, fake=fake_paystack)
    batch = make_batch(db)
    suspended.profile_status = "suspended"
    db.query(CaregiverBankAccount).filter_by(caregiver_id=changed.id).update(
        {"account_number": "0000000009", "recipient_code": "RCP_new", "payout_blocked_until": MONDAY + timedelta(hours=20)})
    db.commit()
    approve(db, batch)
    assert payouts_of(db, ok)[0].status == "processing"
    for cg, amount in ((suspended, 700_000), (changed, 600_000)):
        (p,) = payouts_of(db, cg)
        assert p.status == "cancelled" and p.cancelled_by is None and "Not paid when the batch was approved" in p.cancel_reason
        assert ledger(db, cg)[0].status == "cancelled"
        assert wallet_balance(db, cg) == amount  # back in the available balance
        assert_conserved(db, cg, amount)
    assert db.query(AdminAuditLog).filter_by(action="payout.cancel").count() == 2


def test_a_batch_where_nobody_can_be_paid_any_more_ends_up_cancelled(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    cg.profile_status = "suspended"
    db.commit()
    approve(db, batch)
    assert db.get(PayoutBatch, batch.id).status == "cancelled" and wallet_balance(db, cg) == 1_000_000


# --- cancelling --------------------------------------------------------------------------------------------------------


def test_cancelling_returns_the_reserved_money_and_audits(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    other = payable(db, 900_000, fake=fake_paystack)
    batch = make_batch(db)
    (p,) = payouts_of(db, cg)
    admin = make_admin(db, ["super_admin"], password=None)
    payouts.cancel_payout(db, p.id, admin_id=admin.id, reason="Disputed amount", now=MONDAY)
    db.expire_all()
    p = payouts_of(db, cg)[0]
    assert (p.status, p.cancelled_by, p.cancel_reason) == ("cancelled", admin.id, "Disputed amount")
    assert ledger(db, cg)[0].status == "cancelled" and wallet_balance(db, cg) == 1_000_000
    assert db.get(PayoutBatch, batch.id).status == "pending_approval"  # the other payout is still waiting
    assert db.query(AdminAuditLog).filter_by(action="payout.cancel", entity_id=str(p.id)).one().reason == "Disputed amount"
    with pytest.raises(payouts.PayoutConflict, match="already cancelled"):
        payouts.cancel_payout(db, p.id, admin_id=admin.id, reason="again")
    # cancelling the last one cancels the batch, and then it cannot be approved
    (q,) = payouts_of(db, other)
    payouts.cancel_payout(db, q.id, admin_id=admin.id, reason="Disputed amount too")
    assert db.get(PayoutBatch, batch.id).status == "cancelled"
    with pytest.raises(payouts.PayoutConflict):
        approve(db, batch)
    assert_conserved(db, cg, 1_000_000) and assert_conserved(db, other, 900_000)


def test_a_payout_that_has_been_approved_cannot_be_cancelled(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    approve(db, batch)
    (p,) = payouts_of(db, cg)
    with pytest.raises(payouts.PayoutConflict, match="not been approved"):
        payouts.cancel_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="too late")
    assert wallet_balance(db, cg) == 0


def test_approve_and_cancel_racing_never_lose_or_duplicate_money(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    (p,) = payouts_of(db, cg)
    payout_id, batch_id = p.id, batch.id
    admin = make_admin(db, ["super_admin"], password=None)
    admin_id = admin.id
    barrier = threading.Barrier(2)

    def do(fn):
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            return fn(session)
        except payouts.PayoutConflict as exc:
            return exc
        finally:
            session.close()

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(do, lambda s: payouts.approve_batch(s, batch_id, admin_id=admin_id, reason="r")),
                   pool.submit(do, lambda s: payouts.cancel_payout(s, payout_id, admin_id=admin_id, reason="r"))]
        outcomes = [f.result(timeout=60) for f in futures]
    assert sum(isinstance(o, payouts.PayoutConflict) for o in outcomes) in (0, 1)
    (p,) = payouts_of(db, cg)
    assert p.status in ("processing", "cancelled")
    assert wallet_balance(db, cg) == (1_000_000 if p.status == "cancelled" else 0)
    assert_conserved(db, cg, 1_000_000)


# --- sending ---------------------------------------------------------------------------------------------------------


def approved_payout(db, fake, balance=1_000_000):
    cg = payable(db, balance, fake=fake)
    approve(db, make_batch(db))
    return cg, payouts_of(db, cg)[0]


def test_the_send_job_posts_one_transfer_per_payout_with_its_reference(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    assert payouts.dispatch_due(db, now=MONDAY) == 1
    sent = [r for r in fake_paystack.requests if r[0] == "POST" and r[1].endswith("/transfer")]
    assert len(sent) == 1
    body = sent[0][3]
    assert body["amount"] == 1_000_000 and body["recipient"] == p.recipient_code and body["reference"] == p.reference
    assert body["source"] == "balance" and body["currency"] == "NGN"
    db.expire_all()
    p = payouts_of(db, cg)[0]
    assert p.status == "processing" and p.initiated_at == MONDAY and p.transfer_code.startswith("TRF_")
    assert payouts.dispatch_due(db, now=MONDAY) == 0  # sent once: running again sends nothing
    assert len(fake_paystack.transfers) == 1


def test_an_unapproved_payout_is_never_sent(db, fake_paystack):
    payable(db, 1_000_000, fake=fake_paystack)
    make_batch(db)
    assert payouts.dispatch_due(db) == 0 and fake_paystack.transfers == {}


def test_two_send_workers_at_once_send_each_payout_once(db, fake_paystack):
    for _ in range(4):
        payable(db, 1_000_000, fake=fake_paystack)
    approve(db, make_batch(db))
    barrier = threading.Barrier(2)

    def work():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            return payouts.dispatch_due(session)
        finally:
            session.close()

    with ThreadPoolExecutor(2) as pool:
        sent = [f.result(timeout=60) for f in [pool.submit(work), pool.submit(work)]]
    assert sum(sent) == 4 and len(fake_paystack.transfers) == 4
    assert len([r for r in fake_paystack.requests if r[1].endswith("/transfer") and r[0] == "POST"]) == 4


def test_paystack_refusing_the_transfer_fails_the_payout_and_returns_the_money(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.balance_kobo = 10  # Paystack's own balance is too low
    payouts.dispatch_due(db)
    db.expire_all()
    p = payouts_of(db, cg)[0]
    assert p.status == "failed" and "balance is not enough" in p.failure_reason and p.completed_at is not None
    assert ledger(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 1_000_000
    assert_conserved(db, cg, 1_000_000)


def test_no_answer_from_paystack_leaves_the_payout_processing_with_the_money_reserved(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.offline = True
    payouts.dispatch_due(db)
    db.expire_all()
    p = payouts_of(db, cg)[0]
    assert p.status == "processing" and p.initiated_at is not None and p.transfer_code is None
    assert wallet_balance(db, cg) == 0  # it may have gone: never give the money back on a guess
    fake_paystack.offline = False
    assert payouts.dispatch_due(db) == 0  # and it is not sent a second time by the send job (reconcile asks instead)


def test_a_paystack_server_error_is_treated_like_no_answer(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.transfer_error = (502, "Bad gateway")
    payouts.dispatch_due(db)
    assert payouts_of(db, cg)[0].status == "processing" and wallet_balance(db, cg) == 0


def test_an_otp_transfer_stays_processing_with_a_note_for_the_admin(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.transfer_initial_status = "otp"
    payouts.dispatch_due(db)
    p = payouts_of(db, cg)[0]
    assert p.status == "processing" and "OTP" in p.failure_reason and p.transfer_code
    assert wallet_balance(db, cg) == 0


def test_an_instantly_successful_transfer_settles_at_once(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.transfer_initial_status = "success"
    payouts.dispatch_due(db)
    assert payouts_of(db, cg)[0].status == "success" and ledger(db, cg)[0].status == "successful"


# --- settling by webhook ---------------------------------------------------------------------------------------------


def webhook(client, fake, reference, outcome):
    return client.post(WEBHOOK, **signed(fake.settle_transfer(reference, outcome)))


def test_a_success_webhook_marks_the_payout_and_the_ledger_row_paid(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    assert webhook(client, fake_paystack, p.reference, "success").status_code == 200
    p = payouts_of(db, cg)[0]
    assert p.status == "success" and p.completed_at is not None and ledger(db, cg)[0].status == "successful"
    assert wallet_balance(db, cg) == 0
    assert_conserved(db, cg, 1_000_000)


def test_a_failed_webhook_returns_the_money(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    assert webhook(client, fake_paystack, p.reference, "failed").status_code == 200
    p = payouts_of(db, cg)[0]
    assert p.status == "failed" and ledger(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 1_000_000
    assert_conserved(db, cg, 1_000_000)


def test_a_reversed_webhook_for_a_payout_that_was_never_paid_returns_the_money(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    assert webhook(client, fake_paystack, p.reference, "reversed").status_code == 200
    assert payouts_of(db, cg)[0].status == "reversed" and wallet_balance(db, cg) == 1_000_000
    assert_conserved(db, cg, 1_000_000)


def test_a_reversal_after_success_does_not_return_the_money_by_itself(db, client, fake_paystack):
    """Paystack's two answers disagree, so the caregiver is frozen and the money waits for an admin (see the hardening tests)."""
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "success")
    assert webhook(client, fake_paystack, p.reference, "reversed").status_code == 200
    assert payouts_of(db, cg)[0].status == "needs_attention" and wallet_balance(db, cg) == 0


def test_webhooks_are_idempotent_whatever_the_repeat(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    for _ in range(3):
        assert webhook(client, fake_paystack, p.reference, "failed").status_code == 200
    assert wallet_balance(db, cg) == 1_000_000  # returned once, not three times
    # a late "success" after a failure means Paystack paid: the returned money is taken back and a person is asked to look
    # (the whole story is in test_payout_hardening.py)
    assert webhook(client, fake_paystack, p.reference, "success").status_code == 200
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and wallet_balance(db, cg) == 0 and "after" in q.failure_reason


def test_repeated_success_webhooks_settle_once(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack, balance=900_000)
    payouts.dispatch_due(db)
    for _ in range(3):
        assert webhook(client, fake_paystack, p.reference, "success").status_code == 200
    assert payouts_of(db, cg)[0].status == "success" and wallet_balance(db, cg) == 0
    assert_conserved(db, cg, 900_000)


def test_concurrent_failed_webhooks_return_the_money_once(db, fake_paystack):
    """Deterministic: while a rival transaction holds the payout and its ledger row, three webhook handlers pile up behind it.
    Whoever gets the lock first must change the payout and the others must then see the new state, not the one they read."""
    import time

    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    reference, payout_id, tx_id = p.reference, p.id, p.transaction_id

    def apply():
        session = SessionLocal()
        try:
            return payouts.apply_transfer_event(session, reference, "failed", reason="x")
        finally:
            session.close()

    locker = SessionLocal()
    try:
        locker.query(Payout).filter(Payout.id == payout_id).with_for_update().all()
        locker.query(CaregiverTransaction).filter(CaregiverTransaction.id == tx_id).with_for_update().all()
        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(apply) for _ in range(3)]
            time.sleep(1.0)
            assert not any(f.done() for f in futures), "the handlers did not wait for the payout lock"
            locker.rollback()
            results = sorted(f.result(timeout=60) for f in futures)
    finally:
        locker.close()
    assert results == ["applied", "duplicate", "duplicate"]
    assert wallet_balance(db, cg) == 1_000_000
    assert_conserved(db, cg, 1_000_000)


def test_a_webhook_with_a_bad_signature_changes_nothing(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    body = fake_paystack.settle_transfer(p.reference, "failed")
    r = client.post(WEBHOOK, json=body, headers={"x-paystack-signature": "0" * 128})
    assert r.status_code == 400
    assert payouts_of(db, cg)[0].status == "processing" and wallet_balance(db, cg) == 0


def test_a_webhook_for_a_transfer_that_is_not_ours_is_acknowledged_and_ignored(db, client, fake_paystack):
    body = {"event": "transfer.success", "data": {"reference": "someone-elses-transfer", "amount": 100, "status": "success"}}
    assert client.post(WEBHOOK, **signed(body)).status_code == 200


def test_a_success_for_the_wrong_amount_is_not_settled_and_needs_attention(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    body = fake_paystack.settle_transfer(p.reference, "success")
    body["data"]["amount"] = 1
    assert client.post(WEBHOOK, **signed(body)).status_code == 200
    assert payouts_of(db, cg)[0].status == "needs_attention" and wallet_balance(db, cg) == 0


# --- reconcile -------------------------------------------------------------------------------------------------------


def test_reconcile_settles_a_transfer_whose_webhook_never_came(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db, now=MONDAY)
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=5)) == 0  # too soon to ask
    fake_paystack.settle_transfer(p.reference, "success")
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=16)) == 1
    assert payouts_of(db, cg)[0].status == "success"
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=30)) == 0
    assert_conserved(db, cg, 1_000_000)


def test_reconcile_returns_the_money_of_a_failed_transfer(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db, now=MONDAY)
    fake_paystack.settle_transfer(p.reference, "failed")
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=16)) == 1
    assert payouts_of(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 1_000_000


def test_reconcile_leaves_a_pending_transfer_alone(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db, now=MONDAY)
    assert payouts.reconcile(db, now=MONDAY + timedelta(hours=3)) == 0
    assert payouts_of(db, cg)[0].status == "processing" and wallet_balance(db, cg) == 0


def test_a_transfer_paystack_never_heard_of_is_flagged_only_after_the_grace_period_and_its_money_stays_reserved(db, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    fake_paystack.offline = True
    payouts.dispatch_due(db, now=MONDAY)  # no answer: it may or may not exist
    fake_paystack.offline = False
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=20)) == 0  # not found, but it is early
    assert payouts_of(db, cg)[0].status == "processing"
    assert payouts.reconcile(db, now=MONDAY + timedelta(minutes=31)) == 1
    q = payouts_of(db, cg)[0]
    assert q.status == "needs_attention" and "no record" in q.failure_reason and wallet_balance(db, cg) == 0


# --- retry -----------------------------------------------------------------------------------------------------------


def test_retry_reserves_the_money_again_with_a_new_reference_and_is_sent_again(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "failed")
    first_reference = p.reference
    admin = make_admin(db, ["super_admin"], password=None)
    payouts.retry_payout(db, p.id, admin_id=admin.id, reason="Paystack balance topped up", now=MONDAY)
    db.expire_all()
    q = payouts_of(db, cg)[0]
    assert q.status == "processing" and q.attempts == 2 and q.reference != first_reference and q.reference.endswith("-2")
    assert q.transfer_code is None and q.initiated_at is None and q.failure_reason is None
    assert ledger(db, cg)[0].status == "pending" and ledger(db, cg)[0].reference == q.reference
    assert wallet_balance(db, cg) == 0
    assert db.query(AdminAuditLog).filter_by(action="payout.retry", entity_id=str(p.id)).one().reason == "Paystack balance topped up"
    payouts.dispatch_due(db)
    assert len(fake_paystack.transfers) == 2  # one per attempt, each with its own reference
    webhook(client, fake_paystack, q.reference, "success")
    assert payouts_of(db, cg)[0].status == "success"
    assert_conserved(db, cg, 1_000_000)


def test_retry_works_for_a_reversed_payout(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "reversed")
    payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="again")
    assert payouts_of(db, cg)[0].status == "processing" and wallet_balance(db, cg) == 0


@pytest.mark.parametrize("make_state", ["processing", "success", "cancelled", "pending_approval"])
def test_only_a_failed_or_reversed_payout_can_be_retried(db, client, fake_paystack, make_state):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    batch = make_batch(db)
    (p,) = payouts_of(db, cg)
    admin = make_admin(db, password=None)
    if make_state == "cancelled":
        payouts.cancel_payout(db, p.id, admin_id=admin.id, reason="x")
    elif make_state != "pending_approval":
        approve(db, batch)
        if make_state == "success":
            payouts.dispatch_due(db)
            webhook(client, fake_paystack, p.reference, "success")
    with pytest.raises(payouts.PayoutConflict, match="failed or reversed"):
        payouts.retry_payout(db, p.id, admin_id=admin.id, reason="no")


def test_retry_is_refused_when_the_balance_is_no_longer_there(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "failed")
    db.query(CaregiverWallet).update({"balance": 10})
    db.commit()
    with pytest.raises(payouts.PayoutConflict, match="lower than this payout"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")
    assert payouts_of(db, cg)[0].status == "failed" and wallet_balance(db, cg) == 10


def test_retry_is_refused_when_the_bank_account_changed_or_the_caregiver_is_suspended(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "failed")
    cg.profile_status = "suspended"
    db.commit()
    with pytest.raises(payouts.PayoutConflict, match="suspended"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")
    cg.profile_status = "approved"
    db.query(CaregiverBankAccount).update({"recipient_code": "RCP_changed", "account_number": "0000000008"})
    db.commit()
    with pytest.raises(payouts.PayoutConflict, match="changed after"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")


def test_retry_is_refused_when_the_caregiver_already_has_another_live_payout(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "failed")
    # next week's batch pays the returned money in a new payout ...
    assert payouts.create_batch(db, period_key="2026-10-19", now=MONDAY + timedelta(days=7)) is not None
    # ... so the old one cannot also be sent
    with pytest.raises(payouts.PayoutConflict):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="x")
    assert_conserved(db, cg, 1_000_000)


# --- the admin API -----------------------------------------------------------------------------------------------------


def test_the_admin_api_end_to_end(db, client, fake_paystack):
    payable(db, 1_000_000, fake=fake_paystack)
    payable(db, 2_000_000, fake=fake_paystack)
    batch = make_batch(db)
    finance = make_admin(db, ["finance"], password=None)
    boss = make_admin(db, ["super_admin"], password=None)

    listing = client.get(f"{ADMIN}/batches", headers=auth_headers(finance)).json()
    assert listing["total"] == 1 and listing["items"][0]["total_kobo"] == 3_000_000
    assert listing["items"][0]["counts"] == {"pending_approval": 2} and listing["items"][0]["payout_count"] == 2
    detail = client.get(f"{ADMIN}/batches/{batch.id}", headers=auth_headers(finance)).json()
    assert len(detail["payouts"]) == 2 and all(p["account_number"].startswith("******") for p in detail["payouts"])
    assert client.get(f"{ADMIN}?status=pending_approval&batch_id={batch.id}", headers=auth_headers(finance)).json()["total"] == 2

    # Finance can look but not approve; approving needs ledger.adjust and a reason
    assert client.post(f"{ADMIN}/batches/{batch.id}/approve", json={"reason": "ok"}, headers=auth_headers(finance)).status_code == 403
    assert client.post(f"{ADMIN}/batches/{batch.id}/approve", json={"reason": "  "}, headers=auth_headers(boss)).status_code == 422
    assert client.post(f"{ADMIN}/batches/{batch.id}/approve", json={}, headers=auth_headers(boss)).status_code == 422
    r = client.post(f"{ADMIN}/batches/{batch.id}/approve", json={"reason": "Totals match Finance"}, headers=auth_headers(boss))
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert {p["status"] for p in r.json()["payouts"]} == {"processing"}
    assert client.post(f"{ADMIN}/batches/{batch.id}/approve", json={"reason": "again"}, headers=auth_headers(boss)).status_code == 409
    assert client.post(f"{ADMIN}/batches/{batch.id}/approve", json={"reason": "x"}, headers=auth_headers(boss)).json()["detail"] \
        == "This batch is already approved"
    assert client.get(f"{ADMIN}/batches/{batch.id}".replace(str(batch.id), "00000000-0000-0000-0000-000000000000"),
                      headers=auth_headers(boss)).status_code == 404


def test_cancel_and_retry_through_the_api(db, client, fake_paystack):
    cg1, cg2 = payable(db, 1_000_000, fake=fake_paystack), payable(db, 2_000_000, fake=fake_paystack)
    batch = make_batch(db)
    boss = make_admin(db, ["super_admin"], password=None)
    finance = make_admin(db, ["finance"], password=None)
    p1, p2 = payouts_of(db, cg1)[0], payouts_of(db, cg2)[0]
    assert client.post(f"{ADMIN}/{p1.id}/cancel", json={"reason": "x"}, headers=auth_headers(finance)).status_code == 403
    r = client.post(f"{ADMIN}/{p1.id}/cancel", json={"reason": "Caregiver asked us to hold it"}, headers=auth_headers(boss))
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["cancelled_by"] == str(boss.id)
    assert client.post(f"{ADMIN}/{p1.id}/cancel", json={"reason": "x"}, headers=auth_headers(boss)).status_code == 409
    client.post(f"{ADMIN}/batches/{batch.id}/approve", json={"reason": "ok"}, headers=auth_headers(boss))
    payouts.dispatch_due(db)
    assert client.post(f"{ADMIN}/{p2.id}/retry", json={"reason": "x"}, headers=auth_headers(boss)).status_code == 409  # not failed
    webhook(client, fake_paystack, p2.reference, "failed")
    r = client.post(f"{ADMIN}/{p2.id}/retry", json={"reason": "Paystack topped up"}, headers=auth_headers(boss))
    assert r.status_code == 200 and r.json()["status"] == "processing" and r.json()["attempts"] == 2
    assert client.post(f"{ADMIN}/{p2.id}/retry", json={"reason": "x"}, headers=auth_headers(boss)).status_code == 409
    assert client.post(f"{ADMIN}/00000000-0000-0000-0000-000000000000/cancel", json={"reason": "x"},
                       headers=auth_headers(boss)).status_code == 404


def test_the_admin_list_is_filterable_and_masks_account_numbers(db, client, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack, number="0123456789")
    make_batch(db)
    boss = make_admin(db, ["super_admin"], password=None)
    body = client.get(f"{ADMIN}?caregiver_id={cg.id}", headers=auth_headers(boss)).json()
    assert body["total"] == 1 and body["items"][0]["account_number"] == "******6789"
    assert "0123456789" not in str(body)
    assert client.get(f"{ADMIN}?status=success", headers=auth_headers(boss)).json()["total"] == 0


# --- the caregiver's view, and erasure ---------------------------------------------------------------------------------


def test_the_wallet_shows_the_reservation_and_the_schedule(db, client, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    h = auth_headers(cg.user)
    w = client.get("/api/v1/caregiver/wallet", headers=h).json()
    assert w["balance"] == w["available_balance"] == 1_000_000 and w["payout_in_progress_kobo"] == 0
    assert w["payout_cadence"] == "weekly" and w["payout_min_kobo"] == 500_000 and w["payouts_blocked_until"] is None
    assert date.fromisoformat(w["next_payout_on"]).weekday() == 0
    make_batch(db)
    w = client.get("/api/v1/caregiver/wallet", headers=h).json()
    assert w["available_balance"] == 0 and w["payout_in_progress_kobo"] == 1_000_000 and w["total_withdrawn"] == 0
    txs = client.get("/api/v1/caregiver/wallet/transactions", headers=h).json()
    assert [(t["type"], t["status"], t["amount"]) for t in txs] == [("payout", "pending_approval", 1_000_000)]
    assert txs[0]["account_name"] == "PLACEHOLDER ACCOUNT"


def test_a_paid_payout_counts_in_total_withdrawn(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "success")
    w = client.get("/api/v1/caregiver/wallet", headers=auth_headers(cg.user)).json()
    assert w["total_withdrawn"] == 1_000_000 and w["payout_in_progress_kobo"] == 0 and w["available_balance"] == 0


def test_a_caregiver_with_a_payout_in_flight_cannot_delete_their_account(db, client, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    make_batch(db)
    r = client.delete("/api/v1/auth/profile", headers=auth_headers(cg.user))
    assert r.status_code == 409 and "payout" in r.json()["detail"]
    db.expire_all()
    assert cg.user.is_active is True


def test_erasure_masks_the_payout_destination_and_history(db, client, fake_paystack):
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "success")
    r = client.delete("/api/v1/auth/profile", headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    q = payouts_of(db, cg)[0]
    assert q.account_number.startswith("******") and q.account_name == "Deleted user" and q.recipient_code == "erased"
    assert q.status == "success" and q.amount_kobo == 1_000_000  # the money record stays


def test_an_erased_caregiver_is_not_reserved_even_if_a_batch_was_already_listing_them(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    cg.user.is_active = False  # erased between the candidate list and the lock
    db.commit()
    assert make_batch(db) is None and wallet_balance(db, cg) == 1_000_000
