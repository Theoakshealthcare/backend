"""Refunds v2, final whole-branch review fixes (I1 to I4 and the minors).

I1  Paystack money that left outside Oakly's refund rows is counted, so a later refund cannot push the total past what was paid.
I2  A ``processed`` for an older attempt is recorded on that attempt, and no later action pays the refund again.
I3  A refund in review that Oakly never sent can always be resolved, both ways.
I4  The new payment statuses are "already paid" wherever PAID was; staff refunds only on completed or cancelled visits.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func

from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.patient import PatientTransaction
from app.db.models.refund import Refund
from app.services import refunds
from app.services.booking_payment import SettleOutcome, settle_booking_payment, verify_and_settle
from app.tests.factories import make_admin, make_booking, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)
from app.tests.refund_helpers import assert_refund_conserved, refunds_of
from app.tests.test_money_webhook import _post as post_charge_event
from app.tests.test_refunds_failures import (
    TOTAL, attempts_of, dashboard_refund, failed, finance, get, sent_refund, super_admin)


def paid_booking(db, fake, *, channel="app", total=TOTAL, status=BookingStatus.COMPLETED, **fields):
    booking = make_booking(db, make_patient(db), total_amount_kobo=total, channel=channel, status=status, **fields)
    fake.initialize_transaction(None, "placeholder@example.com", total, booking.paystack_reference)
    fake.paid.add(booking.paystack_reference)
    return booking


def pending_refund(db, booking, suggested=TOTAL, **fields):
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=suggested, status="pending_approval", **fields)
    db.add(refund)
    db.commit()
    return refund


def event(db, booking, status, pid, amount):
    return refunds.apply_refund_event(db, status=status, transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=amount)


def committed(db, booking):
    db.expire_all()
    return int(db.query(func.coalesce(func.sum(Refund.amount_kobo), 0))
               .filter(Refund.booking_id == booking.id, Refund.status.in_(refunds.REFUND_COMMITTED_STATUSES)).scalar())


# --- I1 (a): Paystack refunded a payment Oakly has no open refund for ------------------------------------------------------
def test_a_refund_made_outside_oakly_becomes_a_committed_needs_review_refund(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    pid = dashboard_refund(fake_paystack, booking, "processed", 1_000_000)
    assert event(db, booking, "processed", pid, 1_000_000) == "review"
    [refund] = refunds_of(db, booking)
    assert (refund.origin, refund.reason_code, refund.status) == ("system", "other", "needs_review")
    assert (refund.amount_kobo, refund.suggested_amount_kobo) == (1_000_000, 1_000_000)
    assert refund.review_flagged_at is not None and "outside Oakly" in refund.review_reason
    [attempt] = attempts_of(db, refund)
    assert (attempt.attempt, attempt.paystack_refund_id, attempt.status) == (1, pid, "processed")
    assert refunds.refundable_kobo(db, booking) == TOTAL - 1_000_000
    assert event(db, booking, "processed", pid, 1_000_000) == "review"  # a redelivery makes no second row
    assert len(refunds_of(db, booking)) == 1


def test_a_dashboard_refund_cannot_be_over_refunded_through_a_later_wallet_approval(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    pid = dashboard_refund(fake_paystack, booking, "processed", 1_000_000)
    event(db, booking, "processed", pid, 1_000_000)
    [flagged] = refunds_of(db, booking)
    refunds.resolve_refund(db, flagged.id, admin=super_admin(db), outcome="succeeded", note="made on the dashboard")
    assert get(db, booking).payment_status == PaymentStatus.PARTIALLY_REFUNDED
    again = pending_refund(db, booking, TOTAL)
    with pytest.raises(refunds.RefundInvalid):  # the full amount is more than what is left
        refunds.approve_refund(db, again.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    out = refunds.approve_refund(db, again.id, admin=super_admin(db), amount_kobo=TOTAL - 1_000_000, destination="wallet")
    assert out.status == "succeeded"
    assert_refund_conserved(db, booking)
    assert committed(db, booking) == TOTAL


def test_a_second_outside_refund_while_one_is_in_review_gets_its_own_counted_row(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    first = dashboard_refund(fake_paystack, booking, "processed", 1_000_000)
    second = dashboard_refund(fake_paystack, booking, "processed", 500_000)
    event(db, booking, "processed", first, 1_000_000)
    assert event(db, booking, "processed", second, 500_000) == "review"
    rows = refunds_of(db, booking)
    assert sorted((r.status, r.amount_kobo) for r in rows) == [("closed", 500_000), ("needs_review", 1_000_000)]
    assert refunds.refundable_kobo(db, booking) == TOTAL - 1_500_000  # the index is not violated, both are counted
    assert event(db, booking, "processed", second, 500_000) in ("review", "duplicate")  # a redelivery makes no new row
    assert len(refunds_of(db, booking)) == 2


def test_an_outside_refund_with_an_open_refund_is_counted_survives_resolve_and_is_acknowledged_by_resolve(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    open_one = pending_refund(db, booking, 500_000)
    pid = dashboard_refund(fake_paystack, booking, "processed", 1_000_000)
    assert event(db, booking, "processed", pid, 1_000_000) == "review"
    [outside] = [r for r in refunds_of(db, booking) if r.id != open_one.id]
    assert (outside.status, outside.amount_kobo, outside.origin, outside.reason_code) == ("closed", 1_000_000, "system", "other")
    assert outside.review_flagged_at is not None and "outside Oakly" in outside.review_reason
    assert [(a.paystack_refund_id, a.status) for a in attempts_of(db, outside)] == [(pid, "processed")]
    assert refunds.refundable_kobo(db, booking) == TOTAL - 1_000_000
    with pytest.raises(refunds.RefundConflict):  # Paystack shows it processed: it cannot be called failed
        refunds.resolve_refund(db, outside.id, admin=super_admin(db), outcome="failed", note="no")
    refunds.resolve_refund(db, open_one.id, admin=super_admin(db), outcome="failed", note="never sent")
    o = get(db, outside)
    assert o.status == "closed" and o.review_flagged_at is not None  # resolving the open one did not wipe it
    assert refunds.refundable_kobo(db, booking) == TOTAL - 1_000_000
    out = refunds.resolve_refund(db, outside.id, admin=super_admin(db), outcome="succeeded", note="seen on the dashboard")
    assert out.status == "succeeded" and out.processed_at is not None and out.review_flagged_at is None
    assert get(db, booking).payment_status == PaymentStatus.PARTIALLY_REFUNDED
    assert refunds.refundable_kobo(db, booking) == TOTAL - 1_000_000  # counted once


def test_a_closed_refund_paid_by_paystack_plus_an_open_refund_cannot_be_over_refunded_to_the_wallet(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)  # a TOTAL refund whose attempt failed, then is closed
    refunds.close_refund(db, refund.id, admin=finance(db), reason="stop")
    open_one = refunds.request_refund(db, booking_id=booking.id, amount_kobo=100, reason_code="complaint", note=None,
                                      admin=finance(db))
    fake_paystack.refunds[pid]["status"] = "processed"  # Paystack paid it after all
    event(db, booking, "processed", pid, TOTAL)
    assert get(db, refund).status == "closed" and attempts_of(db, refund)[0].status == "processed"
    assert refunds.refundable_kobo(db, booking) == 0
    for amount in (TOTAL, 100):
        with pytest.raises(refunds.RefundInvalid):
            refunds.approve_refund(db, open_one.id, admin=super_admin(db), amount_kobo=amount, destination="wallet")
    assert db.query(PatientTransaction).filter_by(refund_id=open_one.id).count() == 0


# --- I1 (b): a rejected or closed refund that Paystack paid --------------------------------------------------------------
def test_a_rejected_refund_that_paystack_paid_is_committed_and_in_review(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    rejected = pending_refund(db, booking, 1_000_000)
    refunds.reject_refund(db, rejected.id, admin=finance(db), reason="no")
    pid = dashboard_refund(fake_paystack, booking, "processed", 1_000_000)
    assert event(db, booking, "processed", pid, 1_000_000) == "review"
    r = get(db, rejected)
    assert (r.status, r.amount_kobo) == ("needs_review", 1_000_000) and r.review_flagged_at is not None
    assert refunds.refundable_kobo(db, booking) == TOTAL - 1_000_000
    assert event(db, booking, "processed", pid, 1_000_000) == "review" and len(refunds_of(db, booking)) == 1


def test_a_closed_refund_that_paystack_paid_with_another_open_refund_keeps_a_durable_flag(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    refunds.close_refund(db, refund.id, admin=finance(db), reason="stop")
    get(db, booking).status = BookingStatus.COMPLETED
    db.commit()
    other = refunds.request_refund(db, booking_id=booking.id, amount_kobo=100, reason_code="complaint", note=None,
                                   admin=finance(db))
    event(db, booking, "processed", pid, TOTAL)  # the closed refund's own id: found by id
    r, o = get(db, refund), get(db, other)
    assert r.status == "closed" and r.review_flagged_at is not None and o.status == "pending_approval"


# --- I1 (c): a wallet approval asks Paystack first -----------------------------------------------------------------------
def test_a_wallet_approval_is_refused_while_paystack_shows_an_untied_refund(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    dashboard_refund(fake_paystack, booking, "processed", 1_000_000)  # the webhook never reached us
    refund = pending_refund(db, booking, TOTAL - 1_000_000)
    with pytest.raises(refunds.RefundConflict) as e:
        refunds.approve_refund(db, refund.id, admin=super_admin(db), amount_kobo=None, destination="wallet",
                               paystack=refunds.PaystackService())
    assert e.value.message == refunds.OTHER_REFUND
    assert get(db, refund).status == "pending_approval"
    assert db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 0


def test_a_wallet_approval_is_503_when_paystack_cannot_be_reached(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    refund = pending_refund(db, booking)
    fake_paystack.refund_error = "timeout"
    with pytest.raises(refunds.RefundPaystackUnavailable):
        refunds.approve_refund(db, refund.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    assert get(db, refund).status == "pending_approval"


def test_a_wallet_approval_goes_ahead_when_paystack_shows_only_failed_or_our_own_refunds(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    dashboard_refund(fake_paystack, booking, "failed", 1_000_000)
    refund = pending_refund(db, booking)
    out = refunds.approve_refund(db, refund.id, admin=super_admin(db), amount_kobo=None, destination="wallet")
    assert out.status == "succeeded"


def test_an_original_method_approval_does_not_call_paystack(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    refund = pending_refund(db, booking)
    fake_paystack.refund_error = "timeout"  # would be a 503 if it were asked
    assert refunds.approve_refund(db, refund.id, admin=finance(db), amount_kobo=None).status == "approved"


# --- I2: a processed older attempt is recorded, and nothing pays again --------------------------------------------------
def test_an_older_attempt_processed_blocks_resolve_failed_retry_and_switch(db, fake_paystack):
    booking, refund, pid1 = failed(db, fake_paystack)
    refunds.retry_refund(db, refund.id, admin=finance(db))
    assert refunds.dispatch_due(db) == 1
    pid2 = attempts_of(db, refund)[1].paystack_refund_id
    fake_paystack.settle_refund(pid1, "processed")
    assert event(db, booking, "processed", pid1, TOTAL) == "review"
    r = get(db, refund)
    assert r.status == "needs_review"
    assert [a.status for a in attempts_of(db, refund)][0] == "processed"  # recorded on the attempt that was paid
    fake_paystack.settle_refund(pid2, "failed")
    event(db, booking, "failed", pid2, TOTAL)  # dropped: the refund is in review
    with pytest.raises(refunds.RefundConflict):
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="attempt 2 failed")
    for act in (lambda: refunds.retry_refund(db, refund.id, admin=finance(db)),
                lambda: refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))):
        with pytest.raises(refunds.RefundConflict):
            act()
    assert get(db, refund).status == "needs_review" and len(attempts_of(db, refund)) == 2
    assert refunds.dispatch_due(db) == 0
    # the money did leave through attempt 1, so the person can say so
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="attempt 1 paid")
    assert out.status == "succeeded"
    assert attempts_of(db, refund)[0].status == "processed"
    assert get(db, booking).payment_status == PaymentStatus.REFUNDED


def test_resolve_failed_asks_paystack_about_every_attempt_of_the_refund(db, fake_paystack):
    """The processed event for attempt 1 was dropped (the refund was already in review), so only Paystack knows."""
    booking, refund, pid1 = failed(db, fake_paystack)
    refunds.retry_refund(db, refund.id, admin=finance(db))
    refunds.dispatch_due(db)
    pid2 = attempts_of(db, refund)[1].paystack_refund_id
    fake_paystack.settle_refund(pid2, "failed")
    event(db, booking, "failed", pid2, TOTAL)
    r = get(db, refund)
    r.status, r.review_flagged_at, r.review_reason = "needs_review", datetime.utcnow(), "odd"
    db.commit()
    fake_paystack.refunds[pid1]["status"] = "processed"  # no event reached us
    with pytest.raises(refunds.RefundConflict):
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="checked")
    assert get(db, refund).status == "needs_review"
    # the money did leave through attempt 1, so the person can record that
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="attempt 1 paid")
    assert out.status == "succeeded" and out.processed_at is not None
    assert [a.status for a in attempts_of(db, refund)] == ["processed", "failed"]
    assert get(db, booking).payment_status == PaymentStatus.REFUNDED


def test_retry_finds_an_earlier_attempt_processed_at_paystack_and_flags_instead(db, fake_paystack):
    booking, refund, pid1 = failed(db, fake_paystack)
    refunds.retry_refund(db, refund.id, admin=finance(db))
    refunds.dispatch_due(db)
    pid2 = attempts_of(db, refund)[1].paystack_refund_id
    fake_paystack.settle_refund(pid2, "failed")
    event(db, booking, "failed", pid2, TOTAL)
    assert get(db, refund).status == "failed"
    fake_paystack.refunds[pid1]["status"] = "processed"  # the event for attempt 1 never came
    with pytest.raises(refunds.RefundConflict):
        refunds.retry_refund(db, refund.id, admin=finance(db))
    assert get(db, refund).status == "needs_review"
    assert len(attempts_of(db, refund)) == 2 and refunds.dispatch_due(db) == 0


# --- I3: a refund in review that was never sent can be resolved ----------------------------------------------------------
def test_a_pending_refund_flagged_by_an_event_without_an_id_can_be_resolved_as_succeeded(db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    refund = pending_refund(db, booking, TOTAL)
    pid = dashboard_refund(fake_paystack, booking, "processed", 1_000_000)
    assert event(db, booking, "processed", None, 1_000_000) == "review"  # no refund id in the event: matched by reference
    r = get(db, refund)
    assert r.status == "needs_review" and r.amount_kobo is None
    with pytest.raises(refunds.RefundConflict):  # without the amount Oakly cannot tell which Paystack refund is meant
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="dashboard")
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="dashboard",
                                 amount_kobo=1_000_000)
    assert (out.status, out.amount_kobo) == ("succeeded", 1_000_000)
    assert [(a.paystack_refund_id, a.status) for a in attempts_of(db, refund)] == [(pid, "processed")]
    assert get(db, booking).payment_status == PaymentStatus.PARTIALLY_REFUNDED


def test_a_pending_refund_flagged_by_a_dashboard_refund_with_an_id_is_resolved_as_failed_and_the_money_has_its_own_row(
        db, fake_paystack):
    booking = paid_booking(db, fake_paystack)
    refund = pending_refund(db, booking, TOTAL)
    pid = dashboard_refund(fake_paystack, booking, "processed", 1_000_000)
    assert event(db, booking, "processed", pid, 1_000_000) == "review"
    assert get(db, refund).status == "needs_review"
    refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="never sent")
    refunds.close_refund(db, refund.id, admin=finance(db), reason="not needed")
    assert refunds.refundable_kobo(db, booking) == TOTAL - 1_000_000  # the outside money is still counted


def test_a_never_sent_refund_in_review_can_be_failed_then_closed_but_not_retried(db, fake_paystack):
    """The rule: with Paystack showing nothing live and untied, resolve failed turns a never-sent refund into ``failed``; it can
    then only be closed (retry and switch refuse a refund nobody approved an amount for)."""
    booking = paid_booking(db, fake_paystack)
    refund = pending_refund(db, booking, TOTAL)
    pid = dashboard_refund(fake_paystack, booking, "failed", 1_000_000)
    assert event(db, booking, "failed", pid, 1_000_000) == "review"
    assert get(db, refund).status == "needs_review"
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="nothing left the account")
    assert out.status == "failed"
    for act in (lambda: refunds.retry_refund(db, refund.id, admin=finance(db)),
                lambda: refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))):
        with pytest.raises(refunds.RefundConflict):
            act()
    assert refunds.close_refund(db, refund.id, admin=finance(db), reason="not needed").status == "closed"
    assert refunds.refundable_kobo(db, booking) == TOTAL


# --- I4 (a): the refunded statuses are "already paid" -------------------------------------------------------------------
@pytest.mark.parametrize("state", [PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED])
def test_settling_a_refunded_booking_changes_nothing(db, state):
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED, payment_status=state,
                           cancel_reason="user_cancelled", total_amount_kobo=TOTAL)
    assert settle_booking_payment(db, booking.id, TOTAL) == SettleOutcome.ALREADY_PAID
    assert verify_and_settle(db, booking.id) is None
    db.expire_all()
    assert db.get(Booking, booking.id).payment_status == state
    assert refunds_of(db, booking) == []


@pytest.mark.parametrize("state", [PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED])
def test_charge_failed_does_not_cancel_a_refunded_booking(db, client, state):
    booking = make_booking(db, make_patient(db), status=BookingStatus.COMPLETED, payment_status=state, total_amount_kobo=TOTAL)
    assert post_charge_event(client, "charge.failed", booking.paystack_reference, TOTAL).status_code == 200
    db.expire_all()
    fresh = db.get(Booking, booking.id)
    assert (fresh.payment_status, fresh.status) == (state, BookingStatus.COMPLETED)


# --- I4 (b): staff refunds only on completed or cancelled visits ---------------------------------------------------------
@pytest.mark.parametrize("status", [BookingStatus.PENDING, BookingStatus.ACCEPTED, BookingStatus.IN_PROGRESS])
def test_staff_cannot_raise_a_refund_on_a_visit_that_is_not_over(db, status):
    booking = make_booking(db, make_patient(db), status=status)
    with pytest.raises(refunds.RefundInvalid) as e:
        refunds.request_refund(db, booking_id=booking.id, amount_kobo=100, reason_code="other", note=None, admin=make_admin(db))
    assert e.value.message == "A refund can be raised only for a completed or cancelled visit."
    assert refunds_of(db, booking) == []


@pytest.mark.parametrize("status", [BookingStatus.COMPLETED, BookingStatus.CANCELLED])
def test_staff_can_raise_a_refund_on_a_completed_or_cancelled_visit(db, status):
    booking = make_booking(db, make_patient(db), status=status)
    assert refunds.request_refund(db, booking_id=booking.id, amount_kobo=100, reason_code="other", note=None,
                                  admin=make_admin(db)).status == "pending_approval"


# --- minors ------------------------------------------------------------------------------------------------------------
def test_m2_a_create_reply_that_says_processed_settles_with_paystacks_amount(db, fake_paystack, monkeypatch):
    for paystack_amount, expected in ((TOTAL - 1, "needs_review"), (TOTAL, "succeeded")):
        booking = paid_booking(db, fake_paystack, channel="whatsapp", status=BookingStatus.CANCELLED)
        refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                        suggested_amount_kobo=TOTAL, amount_kobo=TOTAL, destination="original_method", status="approved",
                        approved_at=datetime.utcnow())
        db.add(refund)
        db.commit()
        pid = str(9000 + paystack_amount % 7)
        monkeypatch.setattr(refunds.PaystackService, "create_refund", lambda self, ref, amt, p=pid, a=paystack_amount: {
            "outcome": "accepted", "paystack_refund_id": p, "status": "processed", "amount_kobo": a, "message": None})
        assert refunds.dispatch_due(db) == 1
        assert get(db, refund).status == expected


def test_m3_no_zero_refund_row_when_nothing_is_left_to_refund(db):
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED, payment_status=PaymentStatus.PARTIALLY_REFUNDED,
                           total_amount_kobo=TOTAL)
    db.add(Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                  suggested_amount_kobo=TOTAL, amount_kobo=TOTAL, status="succeeded", processed_at=datetime.utcnow()))
    db.commit()
    assert refunds.open_system_refund(db, booking, "caregiver_cancelled", None) is None
    assert len(refunds_of(db, booking)) == 1


def test_m1_old_needs_attention_refunds_do_not_starve_processing_ones(db, fake_paystack):
    stale = []
    for _ in range(3):
        booking, refund, pid = sent_refund(db, fake_paystack)
        r = get(db, refund)
        a = attempts_of(db, refund)[0]
        r.status, a.status = "needs_attention", "needs_attention"
        a.created_at = datetime.utcnow() - timedelta(minutes=500)
        db.commit()
        stale.append(refund)
    booking, refund, pid = sent_refund(db, fake_paystack)
    fake_paystack.refunds[pid]["status"] = "processed"
    attempts_of(db, refund)[0].created_at = datetime.utcnow() - timedelta(minutes=20)
    db.commit()
    for _ in range(3):  # a few runs: every run asks the least recently asked first, so the live one is reached
        refunds.reconcile(db, limit=2)
    assert get(db, refund).status == "succeeded"
