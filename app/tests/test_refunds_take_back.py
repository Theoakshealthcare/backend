"""Refunds v2 task 6: taking a refunded visit's share back from the caregiver (wallet first, then owed, then recovered from
later earnings).

The invariant: a take-back only ever DECREASES what the caregiver can be paid. It never credits, the available balance never
goes below zero, a payout already reserved is never touched, and what cannot be taken is recorded as ``owed_kobo``."""
from datetime import datetime, timedelta

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.caregiver import CaregiverTransaction, CaregiverWallet
from app.db.models.refund import Refund
from app.services import earnings, payouts, refunds
from app.tests.factories import make_admin, make_booking, make_patient
from app.tests.payout_helpers import assert_never_over, money_state, payable, payouts_of
from app.tests.fake_meta import fake_paystack  # noqa: F401
from app.tests.refund_helpers import assert_refund_conserved


@pytest.fixture(autouse=True)
def _paystack(fake_paystack):
    """A wallet approval asks Paystack whether another refund is moving on the payment, so every test here has a fake one."""
    return fake_paystack

TOTAL = 4_400_000
EARNING = 3_200_000


def super_admin(db):
    return make_admin(db, ("super_admin",), password=None)


def visit(db, balance=0, *, earning_status="successful", available_at=None):
    """A caregiver with ``balance`` available and a completed app booking whose earning is EARNING."""
    cg = payable(db, balance)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=TOTAL, channel="app")
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=EARNING, status=earning_status,
                                booking_id=b.id, available_at=available_at))
    db.commit()
    return cg, b


def refund_with_take_back(db, b, amount, take_back=True):
    r = Refund(booking_id=b.id, patient_id=b.patient_id, reason_code="other", origin="system", suggested_amount_kobo=amount,
               status="pending_approval")
    db.add(r)
    db.commit()
    return refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=amount, destination="wallet",
                                  take_back_from_caregiver=take_back, take_back_reason="Caregiver at fault" if take_back else None)


def wallet(db, cg):
    db.expire_all()
    return db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one()


def rows(db, cg, type="refund_take_back"):
    db.expire_all()
    return db.query(CaregiverTransaction).filter_by(caregiver_id=cg.id, type=type).order_by(CaregiverTransaction.created_at).all()


def test_take_back_is_proportional(db):
    cg, b = visit(db, EARNING)
    r = refund_with_take_back(db, b, 2_200_000)
    assert r.status == "succeeded" and r.take_back_kobo == 1_600_000  # 3,200,000 * 2,200,000 / 4,400,000
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (EARNING - 1_600_000, 0)
    (row,) = rows(db, cg)
    assert (row.amount, row.status, row.booking_id, row.status_reason) == (1_600_000, "successful", b.id, f"refund {r.id}")
    assert_never_over(db, cg, EARNING)
    assert_refund_conserved(db, b)


def test_take_back_capped_by_earlier_take_backs_for_the_booking(db):
    cg, b = visit(db, EARNING)
    first = refund_with_take_back(db, b, 4_000_000)  # 3,200,000 * 4,000,000 / 4,400,000 = 2,909,091
    second = refund_with_take_back(db, b, 400_000)   # 290,909 share, but only 290,909 left
    assert first.take_back_kobo == 2_909_091 and second.take_back_kobo == EARNING - 2_909_091
    assert sum(r.amount for r in rows(db, cg)) == EARNING
    assert wallet(db, cg).balance == 0
    assert_never_over(db, cg, EARNING)


def test_take_back_from_wallet_first_then_owed(db):
    cg, b = visit(db, 1_000_000)
    refund_with_take_back(db, b, 2_200_000)
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (0, 600_000)
    (row,) = rows(db, cg)
    assert row.amount == 1_000_000
    assert_never_over(db, cg, EARNING)


def test_owed_is_recovered_from_the_next_release(db):
    cg, b = visit(db, 0)
    refund_with_take_back(db, b, 2_200_000)  # nothing to take: 1,600,000 owed
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (0, 1_600_000)
    assert rows(db, cg) == []  # nothing was debited, so no ledger row yet
    now = datetime.utcnow()
    later = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=TOTAL)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=1_000_000, status="pending_clearance",
                                booking_id=later.id, available_at=now - timedelta(hours=1)))
    db.commit()
    assert earnings.release_due(db, now=now) == 1
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (0, 600_000)  # the whole 1,000,000 went to the debt
    (row,) = rows(db, cg)
    assert (row.amount, row.status, row.booking_id) == (1_000_000, "successful", None)
    assert row.status_reason.startswith("recovered from release of ")
    # a second release pays the remainder and the rest is the caregiver's
    other = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=TOTAL)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=900_000, status="pending_clearance",
                                booking_id=other.id, available_at=now - timedelta(hours=1)))
    db.commit()
    earnings.release_due(db, now=now)
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (300_000, 0)
    assert sum(r.amount for r in rows(db, cg)) == 1_600_000
    assert_never_over(db, cg, 1_000_000 + 900_000)  # the 3,200,000 visit was refunded in full below the take-back


def test_take_back_never_touches_a_reserved_payout(db, fake_paystack):
    cg = payable(db, 1_000_000, fake=fake_paystack)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=TOTAL, channel="app")
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=EARNING, status="successful", booking_id=b.id))
    db.commit()
    batch = payouts.create_scheduled_batch(db, now=datetime(2026, 10, 12, 6, 10))
    assert batch is not None
    before = money_state(db, cg)
    assert before["available"] == 0 and before["reserved"] == 1_000_000
    payout_before = [(p.id, p.status, p.amount_kobo) for p in payouts_of(db, cg)]
    ledger_before = [(t.id, t.status, t.amount) for t in rows(db, cg, "payout")]
    refund_with_take_back(db, b, 2_200_000)
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (0, 1_600_000)
    assert rows(db, cg) == []
    assert [(p.id, p.status, p.amount_kobo) for p in payouts_of(db, cg)] == payout_before
    assert [(t.id, t.status, t.amount) for t in rows(db, cg, "payout")] == ledger_before
    assert money_state(db, cg)["reserved"] == 1_000_000
    assert_never_over(db, cg, 1_000_000)


def test_take_back_on_unreleased_earning_lowers_it_before_release(db):
    now = datetime.utcnow()
    cg, b = visit(db, 0, earning_status="pending_clearance", available_at=now - timedelta(hours=1))
    refund_with_take_back(db, b, 2_200_000)
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (0, 0)  # no debt: the earning itself is lowered
    assert earnings.release_due(db, now=now) == 1
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (EARNING - 1_600_000, 0)
    assert_never_over(db, cg, EARNING - 1_600_000)


def test_late_cancel_compensation_is_never_taken_back(db):
    cg = payable(db, 500_000)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.CANCELLED, total_amount_kobo=TOTAL, channel="app")
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="late_cancel_compensation", amount=500_000, status="successful",
                                booking_id=b.id))
    db.commit()
    r = Refund(booking_id=b.id, patient_id=b.patient_id, reason_code="other", origin="system", suggested_amount_kobo=1_000_000,
               status="pending_approval")
    db.add(r)
    db.commit()
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="wallet",
                               take_back_from_caregiver=True, take_back_reason="x")
    out = refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    assert out.status == "succeeded" and out.take_back_kobo == 0
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (500_000, 0)
    assert rows(db, cg) == []


def sent_with_take_back(db, fake, *, earning_status="successful", balance=EARNING):
    """A sent (processing) original-method refund of a completed visit that carries a 1,600,000 take-back."""
    from app.tests.test_refunds_settle import sent
    booking, refund, pid = sent(db, fake)
    cg = payable(db, balance)
    booking = db.get(type(booking), booking.id)
    booking.status = BookingStatus.COMPLETED
    booking.caregiver_id = cg.id
    earning = CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=EARNING, status=earning_status,
                                   booking_id=booking.id)
    db.add(earning)
    refund = db.get(Refund, refund.id)
    refund.take_back_from_caregiver, refund.take_back_kobo, refund.take_back_reason = True, 1_600_000, "fault"
    db.commit()
    return cg, booking, refund, earning, pid


def test_take_back_applies_only_on_success(db, fake_paystack):
    from app.tests.test_refunds_settle import apply
    cg, booking, refund, earning, pid = sent_with_take_back(db, fake_paystack)
    assert apply(db, booking, "failed", pid, TOTAL, reason="Insufficient balance") == "applied"
    db.expire_all()
    assert db.get(Refund, refund.id).status == "failed"
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (EARNING, 0) and rows(db, cg) == []
    assert_never_over(db, cg, EARNING)


def test_processed_webhook_takes_back_once_and_records_the_split(db, fake_paystack):
    from app.tests.test_refunds_settle import apply
    cg, booking, refund, earning, pid = sent_with_take_back(db, fake_paystack, balance=1_000_000)
    assert apply(db, booking, "processed", pid, TOTAL) == "applied"
    apply(db, booking, "processed", pid, TOTAL)  # a duplicate event
    db.expire_all()
    r = db.get(Refund, refund.id)
    assert (r.status, r.take_back_debited_kobo, r.take_back_owed_kobo) == ("succeeded", 1_000_000, 600_000)
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (0, 600_000) and len(rows(db, cg)) == 1


def test_earning_voided_after_approval_takes_nothing(db, fake_paystack):
    from app.tests.test_refunds_settle import apply
    cg, booking, refund, earning, pid = sent_with_take_back(db, fake_paystack, earning_status="voided", balance=0)
    assert apply(db, booking, "processed", pid, TOTAL) == "applied"
    db.expire_all()
    r = db.get(Refund, refund.id)
    assert (r.status, r.take_back_debited_kobo, r.take_back_owed_kobo) == ("succeeded", 0, 0)
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (0, 0) and rows(db, cg) == []


def test_held_earning_is_reduced_not_made_a_debt(db):
    cg, b = visit(db, 0, earning_status="held")
    r = refund_with_take_back(db, b, 2_200_000)
    db.expire_all()
    earning = db.query(CaregiverTransaction).filter_by(booking_id=b.id, type="earning").one()
    assert (earning.status, earning.amount) == ("held", EARNING - 1_600_000)
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (0, 0)
    (row,) = rows(db, cg)  # a record that moves no money: voided, so it is neither a debit nor an earning
    assert (row.status, row.amount) == ("voided", 1_600_000) and f"reduced unreleased earning {earning.id}" in row.status_reason
    assert (r.take_back_debited_kobo, r.take_back_owed_kobo) == (0, 0)
    bal = earnings.balances(db, cg.id)
    assert bal.held_kobo == EARNING - 1_600_000
    assert_never_over(db, cg, 0)


def test_preview_is_zero_for_a_voided_earning_and_stable_after_a_reduction(db):
    cg, b = visit(db, 0, earning_status="voided")
    r = Refund(booking_id=b.id, patient_id=b.patient_id, reason_code="other", origin="system", status="pending_approval")
    db.add(r)
    db.commit()
    assert refunds.take_back_preview_kobo(db, r, 2_200_000) == 0
    cg2, b2 = visit(db, 0, earning_status="held")
    refund_with_take_back(db, b2, 2_200_000)  # reduces the held earning to 1,600,000
    r2 = Refund(booking_id=b2.id, patient_id=b2.patient_id, reason_code="other", origin="system", status="pending_approval")
    db.add(r2)
    db.commit()
    assert refunds.take_back_preview_kobo(db, r2, 2_200_000) == 1_600_000  # still based on the original 3,200,000


def test_owed_is_netted_when_a_payout_is_reserved(db, fake_paystack):
    cg = payable(db, 0, fake=fake_paystack)
    w = db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one()
    w.balance, w.owed_kobo = 1_300_000, 600_000  # e.g. a failed payout just returned its money
    db.commit()
    batch = payouts.create_scheduled_batch(db, now=datetime(2026, 10, 12, 6, 10))
    assert batch is not None
    (p,) = payouts_of(db, cg)
    assert p.amount_kobo == 700_000
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (0, 0)
    (row,) = rows(db, cg)
    assert (row.amount, row.status) == (600_000, "successful") and f"recovered at payout {p.id}" == row.status_reason


def test_owed_larger_than_the_balance_leaves_no_payout(db, fake_paystack):
    cg = payable(db, 0, fake=fake_paystack)
    w = db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one()
    w.balance, w.owed_kobo = 1_000_000, 1_500_000
    db.commit()
    assert payouts.create_scheduled_batch(db, now=datetime(2026, 10, 12, 6, 10)) is None
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (1_000_000, 1_500_000)  # nothing moved: the debt waits for later earnings


def test_release_nets_owed_across_several_earnings_and_two_caregivers(db):
    now = datetime.utcnow()
    a, b = payable(db, 0), payable(db, 0)
    for cg, owed in ((a, 1_000_000), (b, 0)):
        w = db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one()
        w.owed_kobo = owed
    for cg in (a, b):
        for amount in (400_000, 400_000, 400_000):
            bk = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=TOTAL)
            db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=amount, status="pending_clearance",
                                        booking_id=bk.id, available_at=now - timedelta(hours=1)))
    db.commit()
    assert earnings.release_due(db, now=now) == 6
    wa, wb = wallet(db, a), wallet(db, b)
    assert (wa.balance, wa.owed_kobo) == (200_000, 0)  # 1,200,000 earned, 1,000,000 recovered
    assert (wb.balance, wb.owed_kobo) == (1_200_000, 0) and rows(db, b) == []
    assert sum(r.amount for r in rows(db, a)) == 1_000_000
    assert_never_over(db, a, 1_200_000)
    assert_never_over(db, b, 1_200_000)


def test_take_back_does_not_freeze_payouts(db):
    cg, b = visit(db, 0)
    refund_with_take_back(db, b, 2_200_000)
    w = wallet(db, cg)
    assert w.owed_kobo == 1_600_000 and w.payouts_frozen_at is None
    bal = earnings.balances(db, cg.id)
    assert bal.available_kobo == 0 and bal.pending_kobo == 0 and bal.held_kobo == 0  # wallet API shows no debt field


def test_apply_take_back_returns_the_split_and_does_not_commit(db):
    cg, b = visit(db, 400_000)
    r = Refund(booking_id=b.id, patient_id=b.patient_id, reason_code="other", origin="system", status="approved")
    db.add(r)
    db.flush()
    out = earnings.apply_take_back(db, booking_id=b.id, refund_id=r.id, amount_kobo=1_000_000,
                                   now=datetime.utcnow())
    assert out == (400_000, 600_000)
    db.rollback()
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (400_000, 0)


def test_retry_is_refused_while_the_caregiver_owes_a_refund(db, client, fake_paystack):
    from app.tests.test_payouts import approved_payout, webhook
    cg, p = approved_payout(db, fake_paystack)
    payouts.dispatch_due(db)
    webhook(client, fake_paystack, p.reference, "failed")
    w = db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one()
    w.owed_kobo = 1_600_000
    db.commit()
    balance_before = wallet(db, cg).balance
    with pytest.raises(payouts.PayoutConflict, match=r"owes .*16,000 from a refund"):
        payouts.retry_payout(db, p.id, admin_id=make_admin(db, password=None).id, reason="again")
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (balance_before, 1_600_000)
    (po,) = payouts_of(db, cg)
    assert po.status == "failed" and po.attempts == 1


def test_apply_take_back_lowering_an_earning_says_so_on_the_earning(db):
    cg, b = visit(db, 0, earning_status="held")
    r = refund_with_take_back(db, b, 2_200_000)
    db.expire_all()
    earning = db.query(CaregiverTransaction).filter_by(booking_id=b.id, type="earning").one()
    assert f"reduced by 1600000 for refund {r.id}" in earning.status_reason
