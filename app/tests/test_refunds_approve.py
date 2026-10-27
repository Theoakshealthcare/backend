"""Refunds v2 task 4: approving and rejecting refunds (limits, one destination, the requester rule, wallet credit)."""
import threading

import pytest

from app.db.models.audit import AdminAuditLog
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import CaregiverTransaction
from app.db.models.patient import Patient, PatientTransaction, PatientWallet
from app.db.models.refund import Refund
from app.db.models.user import User
from app.db.models.whatsapp_booking import BookingEvent
from app.services import refunds
from app.tests.factories import make_admin, make_booking, make_caregiver, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)
from app.tests.refund_helpers import assert_refund_conserved


@pytest.fixture(autouse=True)
def _paystack(fake_paystack):
    """A wallet approval asks Paystack whether another refund is moving on the payment, so every test here has a fake one."""
    return fake_paystack

BIG = 10_000_000  # N100,000


def finance(db, **kw):
    return make_admin(db, ("finance",), password=None, **kw)


def super_admin(db):
    return make_admin(db, ("super_admin",), password=None)


def booking_with(db, total=BIG, **kw):
    kw.setdefault("status", BookingStatus.CANCELLED)
    return make_booking(db, make_patient(db), total_amount_kobo=total, **kw)


def pending(db, booking, suggested=None, *, status="pending_approval", amount=None, origin="system", by=None):
    r = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin=origin,
               requested_by_admin_id=by.id if by else None, suggested_amount_kobo=suggested, amount_kobo=amount,
               status=status)
    db.add(r)
    db.commit()
    return r


def audit_rows(db, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == action).all()


def test_finance_approves_up_to_limit(db):
    b = booking_with(db)
    r = pending(db, b, 5_000_000)
    out = refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    assert (out.status, out.amount_kobo, out.destination) == ("approved", 5_000_000, "original_method")
    assert out.approved_by_admin_id is not None and out.approved_at is not None
    assert_refund_conserved(db, b)


def test_finance_above_limit_is_403_and_names_the_limit(db):
    b = booking_with(db)
    r = pending(db, b, 5_000_100)
    with pytest.raises(refunds.RefundForbidden) as e:
        refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    assert "50,000" in e.value.message
    db.expire_all()
    assert db.get(Refund, r.id).status == "pending_approval"
    assert_refund_conserved(db, b)


def test_limit_is_cumulative_per_booking(db):
    b = booking_with(db, payment_status=PaymentStatus.PARTIALLY_REFUNDED)
    pending(db, b, 3_000_000, status="succeeded", amount=3_000_000)
    r = pending(db, b, 2_500_000)
    with pytest.raises(refunds.RefundForbidden):
        refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    # a closed/rejected earlier refund does not count
    b2 = booking_with(db)
    pending(db, b2, 3_000_000, status="rejected", amount=3_000_000)
    r2 = pending(db, b2, 2_500_000)
    assert refunds.approve_refund(db, r2.id, admin=finance(db), amount_kobo=None).status == "approved"
    assert_refund_conserved(db, b)


def test_super_admin_approves_above_limit(db):
    b = booking_with(db)
    r = pending(db, b, 8_000_000)
    assert refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None).status == "approved"
    assert_refund_conserved(db, b)


def test_requester_cannot_approve_own_staff_refund(db):
    b = booking_with(db)
    me, other = finance(db), finance(db)
    r = pending(db, b, 1_000_000, origin="staff", by=me)
    with pytest.raises(refunds.RefundForbidden) as e:
        refunds.approve_refund(db, r.id, admin=me, amount_kobo=None)
    assert e.value.message == "You raised this refund, so someone else must approve it."
    assert refunds.approve_refund(db, r.id, admin=other, amount_kobo=None).status == "approved"
    assert_refund_conserved(db, b)


def test_system_refund_any_finance_can_approve(db):
    b = booking_with(db)
    r = pending(db, b, 1_000_000)
    assert refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None).status == "approved"
    assert_refund_conserved(db, b)


def test_amount_defaults_to_suggestion_and_null_suggestion_requires_amount(db):
    b = booking_with(db)
    r = pending(db, b, None)
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    assert refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=700_000).amount_kobo == 700_000
    b2 = booking_with(db)
    r2 = pending(db, b2, 900_000)
    assert refunds.approve_refund(db, r2.id, admin=finance(db), amount_kobo=None).amount_kobo == 900_000
    assert_refund_conserved(db, b)


@pytest.mark.parametrize("amount", [0, -5, BIG + 1])
def test_amount_out_of_range_is_422(db, amount):
    b = booking_with(db)
    r = pending(db, b, 1_000_000)
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=amount)
    assert_refund_conserved(db, b)


def test_amount_above_refundable_is_422(db):
    b = booking_with(db, payment_status=PaymentStatus.PARTIALLY_REFUNDED)
    pending(db, b, 4_000_000, status="succeeded", amount=4_000_000)
    r = pending(db, b, 1_000_000)
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=6_000_001)
    assert refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=6_000_000).amount_kobo == 6_000_000
    assert_refund_conserved(db, b)


def test_wallet_destination_needs_super_admin_and_an_app_booker(db):
    b = booking_with(db)
    r = pending(db, b, 1_000_000)
    with pytest.raises(refunds.RefundForbidden):
        refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None, destination="wallet")
    wa = booking_with(db, channel="whatsapp")
    r2 = pending(db, wa, 1_000_000)
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r2.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    db.expire_all()
    assert db.get(Refund, r2.id).status == "pending_approval"
    assert db.query(PatientTransaction).filter_by(refund_id=r2.id).count() == 0
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="cash")
    assert_refund_conserved(db, b)


def test_wallet_destination_credits_once_and_succeeds(db):
    b = booking_with(db, refund_required=True)
    r = pending(db, b, 2_500_000)
    out = refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    assert (out.status, out.destination) == ("succeeded", "wallet") and out.processed_at is not None
    db.expire_all()
    wallet = db.query(PatientWallet).filter_by(patient_id=b.patient_id).one()
    assert (wallet.balance_kobo, wallet.total_refunded_kobo) == (2_500_000, 2_500_000)
    [txn] = db.query(PatientTransaction).filter_by(refund_id=r.id).all()
    assert (txn.type, txn.status, txn.amount_kobo, txn.wallet_id) == ("refund", "successful", 2_500_000, wallet.id)
    booking = db.get(Booking, b.id)
    assert booking.payment_status == PaymentStatus.PARTIALLY_REFUNDED and booking.refund_required is False
    with pytest.raises(refunds.RefundConflict):  # a second approve cannot credit again
        refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    db.expire_all()
    assert db.get(PatientWallet, wallet.id).balance_kobo == 2_500_000
    assert_refund_conserved(db, b)


def test_full_wallet_refund_marks_booking_refunded_and_second_refund_notifies_again(db):
    b = booking_with(db, channel="app")
    first = pending(db, b, 4_000_000)
    refunds.approve_refund(db, first.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    second = pending(db, b, 6_000_000)
    refunds.approve_refund(db, second.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    db.expire_all()
    assert db.get(Booking, b.id).payment_status == PaymentStatus.REFUNDED
    assert db.get(PatientWallet, db.query(PatientWallet).filter_by(patient_id=b.patient_id).one().id).balance_kobo == BIG
    assert_refund_conserved(db, b)


def test_notice_is_keyed_per_refund(db):
    b = booking_with(db, channel="whatsapp")
    # WhatsApp bookings cannot take wallet credit, so drive the success routine directly for two refunds.
    for amount in (4_000_000, 6_000_000):
        r = pending(db, b, amount, status="approved", amount=amount)
        booking = db.query(Booking).filter_by(id=b.id).with_for_update().one()
        refunds._mark_succeeded(db, r, booking, __import__("datetime").datetime.utcnow())
        db.commit()
    keys = {e.dedupe_key for e in db.query(BookingEvent).filter_by(booking_id=b.id, event="refund_processed")}
    assert len(keys) == 2 and all(k.endswith(":complete") for k in keys)
    assert_refund_conserved(db, b)


def test_take_back_only_for_completed_and_needs_reason(db):
    b = booking_with(db)  # cancelled
    r = pending(db, b, 1_000_000)
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None, take_back_from_caregiver=True,
                               take_back_reason="x")
    cg = make_caregiver(db)
    done = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=BIG)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=3_000_000, status="successful",
                                booking_id=done.id))
    db.commit()
    r2 = pending(db, done, 1_000_000)
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r2.id, admin=finance(db), amount_kobo=None, take_back_from_caregiver=True,
                               take_back_reason="  ")
    out = refunds.approve_refund(db, r2.id, admin=finance(db), amount_kobo=None, take_back_from_caregiver=True,
                                 take_back_reason="Caregiver at fault")
    assert (out.take_back_from_caregiver, out.take_back_kobo) == (True, 300_000)  # 3,000,000 earning * 1,000,000 / 10,000,000
    assert audit_rows(db, "refund.approve")[-1].after["take_back_kobo"] == 300_000
    assert_refund_conserved(db, done)


def test_take_back_preview_formula_rounds_half_up_and_caps(db):
    cg = make_caregiver(db)
    done = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=3)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=1, status="successful", booking_id=done.id))
    db.commit()
    r = pending(db, done, 2)
    assert refunds.take_back_preview_kobo(db, r, 2) == 1  # 1*2/3 = 0.67 -> 1
    assert refunds.take_back_preview_kobo(db, r, 1) == 0  # 1*1/3 = 0.33 -> 0
    other = booking_with(db)
    assert refunds.take_back_preview_kobo(db, pending(db, other, 1), 1) == 0  # not completed
    nowage = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=BIG)
    assert refunds.take_back_preview_kobo(db, pending(db, nowage, 1), 1) == 0  # no earning row
    # cap: earlier committed take-backs reduce what is left
    cap = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=BIG)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=3_000_000, status="successful", booking_id=cap.id))
    prior = Refund(booking_id=cap.id, patient_id=cap.patient_id, reason_code="other", origin="system", status="succeeded",
                   amount_kobo=9_000_000, take_back_kobo=2_700_000, take_back_from_caregiver=True)
    db.add(prior)
    db.commit()
    assert refunds.take_back_preview_kobo(db, pending(db, cap, 1_000_000), 1_000_000) == 300_000
    # and a refund asking for more than what is left is capped at what is left (3,000,000 * 5/10 = 1,500,000 > 300,000)
    r_big = db.query(Refund).filter_by(booking_id=cap.id, status="pending_approval").one()
    assert refunds.take_back_preview_kobo(db, r_big, 5_000_000) == 300_000
    assert_refund_conserved(db, cap)


def test_approve_twice_is_409(db):
    b = booking_with(db)
    r = pending(db, b, 1_000_000)
    refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    with pytest.raises(refunds.RefundConflict):
        refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    with pytest.raises(refunds.RefundConflict):
        refunds.reject_refund(db, r.id, admin=finance(db), reason="no")
    with pytest.raises(refunds.RefundNotFound):
        refunds.approve_refund(db, __import__("uuid").uuid4(), admin=finance(db), amount_kobo=None)
    assert_refund_conserved(db, b)


def test_reject_needs_reason_and_is_terminal(db):
    b = booking_with(db)
    r = pending(db, b, 1_000_000)
    with pytest.raises(refunds.RefundInvalid):
        refunds.reject_refund(db, r.id, admin=finance(db), reason="  ")
    out = refunds.reject_refund(db, r.id, admin=finance(db), reason="Not eligible")
    assert (out.status, out.rejected_reason) == ("rejected", "Not eligible")
    with pytest.raises(refunds.RefundConflict):
        refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    # a rejected refund frees the booking for a new one
    assert pending(db, b, 1_000_000).status == "pending_approval"
    assert_refund_conserved(db, b)


def test_every_action_writes_an_audit_row(db):
    b = booking_with(db)
    r = pending(db, b, 1_200_000)
    refunds.approve_refund(db, r.id, admin=finance(db), amount_kobo=None)
    [row] = [a for a in audit_rows(db, "refund.approve") if a.entity_id == str(r.id) or str(a.entity_id) == str(r.id)]
    for key, want in (("amount_kobo", 1_200_000), ("destination", "original_method"),
                      ("take_back_from_caregiver", False), ("take_back_kobo", 0), ("limit_kobo", 5_000_000),
                      ("cumulative_kobo", 1_200_000), ("above_limit", False)):
        assert row.after[key] == want
    r2 = pending(db, booking_with(db), 1)
    refunds.reject_refund(db, r2.id, admin=finance(db), reason="duplicate")
    [rej] = [a for a in audit_rows(db, "refund.reject") if str(a.entity_id) == str(r2.id)]
    assert rej.reason == "duplicate"
    big = booking_with(db)
    r3 = pending(db, big, 8_000_000)
    refunds.approve_refund(db, r3.id, admin=super_admin(db), amount_kobo=None)
    [above] = [a for a in audit_rows(db, "refund.approve") if str(a.entity_id) == str(r3.id)]
    assert (above.after["above_limit"], above.after["cumulative_kobo"]) == (True, 8_000_000)
    assert_refund_conserved(db, b)
    assert_refund_conserved(db, big)


def test_system_refund_accepts_a_partially_refunded_booking_and_caps_the_suggestion(db):
    b = booking_with(db, total=4_400_000, payment_status=PaymentStatus.PARTIALLY_REFUNDED)
    b.cancel_fee_tier = "none"
    pending(db, b, 4_000_000, status="succeeded", amount=4_000_000)
    r = refunds.open_system_refund(db, b, "no_caregiver_found", None)
    db.commit()
    assert r is not None and r.suggested_amount_kobo == 400_000  # not the full 4.4m: only 400k is left
    assert_refund_conserved(db, b)


def test_system_refund_suggestion_is_capped_on_a_paid_booking_too(db):
    b = booking_with(db, total=4_400_000)
    pending(db, b, 4_000_000, status="closed", amount=4_000_000)  # closed frees the slot, and is not committed
    r = refunds.open_system_refund(db, b, "no_caregiver_found", None)
    assert r.suggested_amount_kobo == 4_400_000
    assert_refund_conserved(db, b)


def test_concurrent_wallet_approvals_credit_once(db):
    from app.db.session import SessionLocal
    b = booking_with(db, channel="app")
    r = pending(db, b, 1_000_000)
    admin_id = super_admin(db).id
    results = []

    def go():
        s = SessionLocal()
        try:
            from app.db.models.admin import AdminUser
            refunds.approve_refund(s, r.id, admin=s.get(AdminUser, admin_id), amount_kobo=None, destination="wallet")
            results.append("ok")
        except refunds.RefundConflict:
            results.append("conflict")
        finally:
            s.close()

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["conflict", "ok"]
    db.expire_all()
    assert db.query(PatientTransaction).filter_by(refund_id=r.id).count() == 1
    assert db.query(PatientWallet).filter_by(patient_id=b.patient_id).one().balance_kobo == 1_000_000
    assert_refund_conserved(db, b)


def test_inactive_account_cannot_take_wallet_credit(db):
    b = booking_with(db, channel="app")
    owner = db.query(User).join(Patient, Patient.user_id == User.id).filter(Patient.id == b.patient_id).one()
    owner.is_active = False
    db.commit()
    r = pending(db, b, 1_000_000)
    with pytest.raises(refunds.RefundInvalid):
        refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    assert db.query(PatientTransaction).filter_by(refund_id=r.id).count() == 0
    assert_refund_conserved(db, b)


def test_patient_wallet_is_locked_after_the_take_back(db, monkeypatch):
    """Lock order: booking, refund, caregiver rows (take-back), and the patient wallet LAST."""
    cg = make_caregiver(db)
    done = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=BIG, channel="app")
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=3_000_000, status="successful",
                                booking_id=done.id))
    db.commit()
    r = pending(db, done, 1_000_000)
    calls = []
    monkeypatch.setattr(refunds, "_apply_take_back", lambda *a, **k: calls.append("take_back"))
    real = refunds.patient_wallet.credit_refund_locked
    monkeypatch.setattr(refunds.patient_wallet, "credit_refund_locked",
                        lambda *a, **k: (calls.append("wallet"), real(*a, **k))[1])
    refunds.approve_refund(db, r.id, admin=super_admin(db), amount_kobo=None, destination="wallet",
                           take_back_from_caregiver=True, take_back_reason="fault")
    assert calls == ["take_back", "wallet"]
    assert_refund_conserved(db, done)
