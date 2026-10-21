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


def test_take_back_on_unreleased_earning_recovers_at_release(db):
    now = datetime.utcnow()
    cg, b = visit(db, 0, earning_status="pending_clearance", available_at=now - timedelta(hours=1))
    refund_with_take_back(db, b, 2_200_000)
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (0, 1_600_000)
    assert earnings.release_due(db, now=now) == 1
    w = wallet(db, cg)
    assert (w.balance, w.owed_kobo) == (EARNING - 1_600_000, 0)
    (row,) = rows(db, cg)
    assert row.amount == 1_600_000
    assert_never_over(db, cg, EARNING)


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


def test_take_back_applies_only_on_success(db):
    cg, b = visit(db, EARNING)
    r = Refund(booking_id=b.id, patient_id=b.patient_id, reason_code="other", origin="system", suggested_amount_kobo=2_200_000,
               status="pending_approval")
    db.add(r)
    db.commit()
    out = refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=2_200_000, take_back_from_caregiver=True,
                                 take_back_reason="fault")  # original method: approved, not yet sent
    assert out.status == "approved" and out.take_back_kobo == 1_600_000
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (EARNING, 0) and rows(db, cg) == []
    out.status = "failed"  # a failed refund is still open but has taken nothing
    db.commit()
    out.status = "closed"
    db.commit()
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (EARNING, 0) and rows(db, cg) == []
    assert_never_over(db, cg, EARNING)


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
    out = earnings.apply_take_back(db, caregiver_id=cg.id, booking_id=b.id, refund_id=r.id, amount_kobo=1_000_000,
                                   now=datetime.utcnow())
    assert out == (400_000, 600_000)
    db.rollback()
    assert (wallet(db, cg).balance, wallet(db, cg).owed_kobo) == (400_000, 0)
