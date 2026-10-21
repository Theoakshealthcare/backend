"""Refunds v2 task 7: the admin failure actions (retry, bank details, switch to wallet, close, resolve).

The invariants: nothing is sent, credited or settled on Paystack's silence; Paystack is asked with no lock held and the answer
is re-checked under the locks (a refund that moved meanwhile is a 409); the wallet and Paystack can never both pay one refund
without a person being told; the success routine runs at most once per refund."""
import json
import threading
from datetime import datetime

import pytest

from app.db.models.admin import AdminUser
from app.db.models.audit import AdminAuditLog
from app.db.models.booking import Booking, PaymentStatus
from app.db.models.patient import PatientTransaction, PatientWallet
from app.db.models.refund import Refund, RefundAttempt
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.services import refunds
from app.tests.factories import make_admin, make_booking, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)
from app.tests.refund_helpers import assert_refund_conserved

TOTAL = 4_400_000
ACCOUNT = "0123456789"
BANK = "058"


def finance(db):
    return make_admin(db, ("finance",), password=None)


def super_admin(db):
    return make_admin(db, ("super_admin",), password=None)


def sent_refund(db, fake, *, channel="app", amount=TOTAL, total=TOTAL, **booking_fields):
    """An approved original-method refund that has been sent: processing, attempt 1 pending at Paystack."""
    booking = make_booking(db, make_patient(db), total_amount_kobo=total, channel=channel, **booking_fields)
    fake.initialize_transaction(None, "placeholder@example.com", total, booking.paystack_reference)
    fake.paid.add(booking.paystack_reference)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=amount, amount_kobo=amount, destination="original_method", status="approved",
                    approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    assert refunds.dispatch_due(db) == 1
    return booking, refund, attempts_of(db, refund)[0].paystack_refund_id


def settled(db, fake, status, **kw):
    """A sent refund that Paystack then settled as ``status`` (applied through the same path the webhook uses)."""
    booking, refund, pid = sent_refund(db, fake, **kw)
    fake.settle_refund(pid, status)
    assert refunds.apply_refund_event(db, status=status, transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=refund.amount_kobo, reason="boom") == "applied"
    return booking, refund, pid


def failed(db, fake, **kw):
    return settled(db, fake, "failed", **kw)


def reviewed(db, fake, **kw):
    """A refund in needs_review: Paystack processed a different amount than ours."""
    booking, refund, pid = sent_refund(db, fake, **kw)
    assert refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=refund.amount_kobo - 1) == "review"
    return booking, refund, pid


def get(db, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


def attempts_of(db, refund):
    db.expire_all()
    return db.query(RefundAttempt).filter_by(refund_id=refund.id).order_by(RefundAttempt.attempt).all()


def posts(fake):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == "/refund"]


def events(db, booking):
    db.expire_all()
    return sorted((e.event, e.dedupe_key) for e in db.query(BookingEvent).filter_by(booking_id=booking.id))


def audit_rows(db, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == action).all()


# --- retry ----------------------------------------------------------------------------------------------------------
def test_retry_only_from_failed_after_paystack_confirms_failed(db, fake_paystack):
    booking, refund, pid = sent_refund(db, fake_paystack)  # processing
    with pytest.raises(refunds.RefundConflict):
        refunds.retry_refund(db, refund.id, admin=finance(db))
    assert get(db, refund).status == "processing"
    booking2, refund2, pid2 = failed(db, fake_paystack)
    out = refunds.retry_refund(db, refund2.id, admin=finance(db))
    assert out.status == "approved" and out.failure_reason is None
    [row] = audit_rows(db, "refund.retry")
    assert str(row.entity_id) == str(refund2.id)
    assert_refund_conserved(db, booking2)


def test_retry_refused_while_paystack_does_not_say_failed(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    fake_paystack.refunds[pid]["status"] = "processing"  # we recorded failed, Paystack is still working on it
    with pytest.raises(refunds.RefundConflict):
        refunds.retry_refund(db, refund.id, admin=finance(db))
    assert get(db, refund).status == "failed" and len(attempts_of(db, refund)) == 1
    assert refunds.dispatch_due(db) == 0


def test_retry_when_paystack_says_processed_settles_and_refuses(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    fake_paystack.settle_refund(pid, "processed")  # Paystack paid it after all
    with pytest.raises(refunds.RefundConflict):
        refunds.retry_refund(db, refund.id, admin=finance(db))
    out = get(db, refund)
    assert out.status == "needs_review" and out.review_reason  # we had recorded failed: a person confirms (resolve)
    assert len(attempts_of(db, refund)) == 1
    assert refunds.dispatch_due(db) == 0 and len(posts(fake_paystack)) == 1  # never sent again


def test_retry_when_paystack_unreachable_is_503_and_stays_failed(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    with pytest.raises(refunds.RefundPaystackUnavailable) as e:
        refunds.retry_refund(db, refund.id, admin=finance(db))
    assert e.value.message == "We couldn't reach Paystack to confirm the last attempt failed. Try again shortly."
    assert get(db, refund).status == "failed" and audit_rows(db, "refund.retry") == []
    fake_paystack.refund_error = "500"
    with pytest.raises(refunds.RefundPaystackUnavailable):
        refunds.retry_refund(db, refund.id, admin=finance(db))


def test_retry_creates_attempt_two_and_keeps_attempt_one(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    refunds.retry_refund(db, refund.id, admin=finance(db))
    assert refunds.dispatch_due(db) == 1
    one, two = attempts_of(db, refund)
    assert (one.attempt, one.status, one.failure_reason, one.paystack_refund_id) == (1, "failed", "boom", pid)
    assert two.attempt == 2 and two.paystack_refund_id and two.paystack_refund_id != pid
    assert get(db, refund).status == "processing" and len(posts(fake_paystack)) == 2


def test_retry_refuses_when_the_refund_moved_while_paystack_was_asked(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    admin = finance(db)
    real = refunds.PaystackService.fetch_refund

    def moved(self, paystack_refund_id):
        answer = real(self, paystack_refund_id)
        s = SessionLocal()  # someone else closes the refund after Paystack answered and before we lock
        try:
            s.get(Refund, refund.id).status = "closed"
            s.commit()
        finally:
            s.close()
        return answer

    refunds.PaystackService.fetch_refund = moved
    try:
        with pytest.raises(refunds.RefundConflict):
            refunds.retry_refund(db, refund.id, admin=admin)
    finally:
        refunds.PaystackService.fetch_refund = real
    assert get(db, refund).status == "closed" and len(attempts_of(db, refund)) == 1


def test_concurrent_retries_move_it_once(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    admin_id = finance(db).id
    results = []

    def go():
        s = SessionLocal()
        try:
            refunds.retry_refund(s, refund.id, admin=s.get(AdminUser, admin_id))
            results.append("ok")
        except refunds.RefundConflict:
            results.append("conflict")
        finally:
            s.close()

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["conflict", "ok"]
    assert len(audit_rows(db, "refund.retry")) == 1


# --- bank details ---------------------------------------------------------------------------------------------------
def test_bank_details_resolves_name_calls_retry_and_stores_only_last4(db, fake_paystack):
    booking, refund, pid = settled(db, fake_paystack, "needs-attention")
    assert get(db, refund).status == "needs_attention"
    fake_paystack.accounts[(ACCOUNT, BANK)] = "ADA OKAFOR"
    out = refunds.submit_bank_details(db, refund.id, admin=finance(db), bank_code=BANK, account_number=ACCOUNT)
    assert out.status == "processing"
    details = fake_paystack.refunds[pid]["refund_account_details"]
    assert details["account_number"] == ACCOUNT  # Paystack got it
    assert str(details["bank_id"]) == "9"  # the numeric bank id, not the code "058"
    [attempt] = attempts_of(db, refund)
    assert attempt.status == "processing" and attempt.bank_details_submitted_at is not None
    assert (attempt.bank_code, attempt.bank_id, attempt.account_last4, attempt.account_name) == (BANK, "9", "6789", "ADA OKAFOR")
    assert "ADA OKAFOR" not in (get(db, refund).note or "")
    [row] = audit_rows(db, "refund.bank_details")
    blob = json.dumps([row.before, row.after, row.reason], default=str)
    assert "6789" in blob and "ADA OKAFOR" in blob and BANK in blob
    stored = [get(db, refund).__dict__, attempt.__dict__]
    assert ACCOUNT not in blob and ACCOUNT not in json.dumps(stored, default=str)
    assert ACCOUNT not in json.dumps(get(db, refund).note, default=str)
    cols = {c.name: getattr(attempt, c.name) for c in RefundAttempt.__table__.columns}
    assert not any(ACCOUNT in str(v) for v in cols.values())


def test_bank_details_only_from_needs_attention(db, fake_paystack):
    booking, refund, pid = sent_refund(db, fake_paystack)  # processing
    with pytest.raises(refunds.RefundConflict):
        refunds.submit_bank_details(db, refund.id, admin=finance(db), bank_code=BANK, account_number=ACCOUNT)
    assert not [r for r in fake_paystack.requests if "retry_with_customer_details" in r[1]]


def test_bank_details_rejects_a_bad_number_an_unknown_bank_and_an_unresolvable_account(db, fake_paystack):
    booking, refund, pid = settled(db, fake_paystack, "needs-attention")
    admin = finance(db)
    for bank, number in ((BANK, "12345"), ("999", ACCOUNT)):
        with pytest.raises(refunds.RefundInvalid):
            refunds.submit_bank_details(db, refund.id, admin=admin, bank_code=bank, account_number=number)
    fake_paystack.unresolvable.add((ACCOUNT, BANK))
    with pytest.raises(refunds.RefundInvalid):
        refunds.submit_bank_details(db, refund.id, admin=admin, bank_code=BANK, account_number=ACCOUNT)
    assert get(db, refund).status == "needs_attention"
    assert not [r for r in fake_paystack.requests if "retry_with_customer_details" in r[1]]


def test_bank_details_refused_by_paystack_changes_nothing(db, fake_paystack):
    booking, refund, pid = settled(db, fake_paystack, "needs-attention")
    fake_paystack.refunds[pid]["status"] = "processing"  # Paystack moved on: it answers 422 to the retry
    with pytest.raises(refunds.RefundConflict):
        refunds.submit_bank_details(db, refund.id, admin=finance(db), bank_code=BANK, account_number=ACCOUNT)
    assert get(db, refund).status == "needs_attention" and audit_rows(db, "refund.bank_details") == []


# --- switch to wallet -----------------------------------------------------------------------------------------------
def test_switch_to_wallet_super_admin_only_after_confirmed_failure(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack, refund_required=True)
    with pytest.raises(refunds.RefundForbidden):
        refunds.switch_to_wallet(db, refund.id, admin=finance(db))
    assert get(db, refund).status == "failed"
    fake_paystack.refunds[pid]["status"] = "processing"
    with pytest.raises(refunds.RefundConflict):  # Paystack has not confirmed the failure
        refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    fake_paystack.refunds[pid]["status"] = "failed"
    out = refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    assert (out.status, out.destination) == ("succeeded", "wallet") and out.processed_at is not None
    wallet = db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one()
    assert wallet.balance_kobo == TOTAL
    assert db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 1
    b = get(db, booking)
    assert b.payment_status == PaymentStatus.REFUNDED and b.refund_required is False
    with pytest.raises(refunds.RefundConflict):  # the second switch cannot credit again
        refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    assert get(db, wallet).balance_kobo == TOTAL
    assert len(audit_rows(db, "refund.switch_to_wallet")) == 1
    assert_refund_conserved(db, booking)


def test_switch_to_wallet_needs_an_app_booker(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack, channel="whatsapp")
    with pytest.raises(refunds.RefundInvalid):
        refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    assert get(db, refund).status == "failed" and db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 0


def test_switch_to_wallet_unreachable_paystack_is_503_and_credits_nothing(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    with pytest.raises(refunds.RefundPaystackUnavailable):
        refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    assert get(db, refund).status == "failed" and db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 0


def test_concurrent_switches_credit_once(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    admin_id = super_admin(db).id
    results = []

    def go():
        s = SessionLocal()
        try:
            refunds.switch_to_wallet(s, refund.id, admin=s.get(AdminUser, admin_id))
            results.append("ok")
        except refunds.RefundConflict:
            results.append("conflict")
        finally:
            s.close()

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["conflict", "ok"]
    assert db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 1
    assert db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one().balance_kobo == TOTAL


def test_processed_after_switch_to_wallet_goes_to_review(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    fake_paystack.settle_refund(pid, "processed")
    assert refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=TOTAL) == "review"
    out = get(db, refund)
    assert out.status == "needs_review" and out.review_reason and out.processed_at is not None
    assert db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one().balance_kobo == TOTAL  # nothing reversed
    assert db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 1


# --- close ----------------------------------------------------------------------------------------------------------
def test_close_needs_reason_and_allows_a_new_refund_later(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    for blank in ("", "   "):
        with pytest.raises(refunds.RefundInvalid):
            refunds.close_refund(db, refund.id, admin=finance(db), reason=blank)
    assert get(db, refund).status == "failed"
    out = refunds.close_refund(db, refund.id, admin=finance(db), reason="  family asked us to stop  ")
    assert out.status == "closed" and "family asked us to stop" in (out.note or "")
    [row] = audit_rows(db, "refund.close")
    assert row.reason == "family asked us to stop"
    again = refunds.request_refund(db, booking_id=booking.id, amount_kobo=TOTAL, reason_code="complaint", note=None,
                                   admin=finance(db))
    assert again.status == "pending_approval"
    with pytest.raises(refunds.RefundConflict):  # only a failed refund can be closed
        refunds.close_refund(db, refund.id, admin=finance(db), reason="again")
    assert_refund_conserved(db, booking)


def test_close_only_from_failed(db, fake_paystack):
    booking, refund, pid = sent_refund(db, fake_paystack)
    with pytest.raises(refunds.RefundConflict):
        refunds.close_refund(db, refund.id, admin=finance(db), reason="no")
    assert get(db, refund).status == "processing"


# --- resolve --------------------------------------------------------------------------------------------------------
def test_resolve_needs_super_admin(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack)
    with pytest.raises(refunds.RefundForbidden):
        refunds.resolve_refund(db, refund.id, admin=finance(db), outcome="succeeded", note="checked")
    assert get(db, refund).status == "needs_review"


def test_resolve_validates_outcome_note_and_state(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack)
    sa = super_admin(db)
    with pytest.raises(refunds.RefundInvalid):
        refunds.resolve_refund(db, refund.id, admin=sa, outcome="maybe", note="checked")
    with pytest.raises(refunds.RefundInvalid):
        refunds.resolve_refund(db, refund.id, admin=sa, outcome="failed", note="  ")
    b2, r2, p2 = sent_refund(db, fake_paystack)
    with pytest.raises(refunds.RefundConflict):  # not in review
        refunds.resolve_refund(db, r2.id, admin=sa, outcome="failed", note="checked")


def test_resolve_succeeded_applies_success_effects_once(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack, refund_required=True, channel="whatsapp")
    fake_paystack.settle_refund(pid, "processed")
    fake_paystack.refunds[pid]["amount"] = TOTAL  # the dashboard shows the amount we expected
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="matches the dashboard")
    assert out.status == "succeeded" and out.processed_at is not None
    assert (out.review_flagged_at, out.review_reason, out.failure_reason) == (None, None, None)
    b = get(db, booking)
    assert b.payment_status == PaymentStatus.REFUNDED and b.refund_required is False and b.refund_amount_kobo == TOTAL
    assert events(db, booking).count(("refund_processed", f"{refund.id}:complete")) == 1
    assert [a.status for a in attempts_of(db, refund)] == ["processed"]
    with pytest.raises(refunds.RefundConflict):
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="again")
    assert events(db, booking).count(("refund_processed", f"{refund.id}:complete")) == 1
    [row] = audit_rows(db, "refund.resolve")
    assert row.reason == "matches the dashboard"
    assert_refund_conserved(db, booking)


def test_resolve_succeeded_refused_when_paystack_does_not_show_processed(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack)
    fake_paystack.refunds[pid]["status"] = "failed"
    with pytest.raises(refunds.RefundConflict):
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="trust me")
    out = get(db, refund)
    assert out.status == "needs_review" and out.processed_at is None
    assert get(db, booking).payment_status == PaymentStatus.PAID


def test_resolve_refused_when_paystack_is_unreachable(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    with pytest.raises(refunds.RefundPaystackUnavailable):
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="checked")
    assert get(db, refund).status == "needs_review"


def test_resolve_failed_returns_to_failed(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack)
    fake_paystack.refunds[pid]["status"] = "failed"
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="dashboard says failed")
    assert out.status == "failed" and out.review_flagged_at is None and out.review_reason is None
    assert [a.status for a in attempts_of(db, refund)] == ["failed"] and out.processed_at is None
    assert get(db, booking).payment_status == PaymentStatus.PAID
    # and the usual follow-ups work
    assert refunds.retry_refund(db, refund.id, admin=finance(db)).status == "approved"


def test_resolve_failed_refused_when_paystack_shows_processed(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack)
    fake_paystack.refunds[pid]["status"] = "processed"
    with pytest.raises(refunds.RefundConflict):
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="checked")
    assert get(db, refund).status == "needs_review"


def test_resolve_with_no_attempts_adopts_the_dashboard_refund(db, fake_paystack):
    """A refund made on the Paystack dashboard: ours has no attempt, the contradiction put it in review."""
    booking = make_booking(db, make_patient(db), total_amount_kobo=TOTAL, channel="whatsapp")
    fake_paystack.initialize_transaction(None, "placeholder@example.com", TOTAL, booking.paystack_reference)
    fake_paystack.paid.add(booking.paystack_reference)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=TOTAL, amount_kobo=TOTAL, destination="original_method", status="approved",
                    approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    dashboard = fake_paystack._create_refund({"transaction": booking.paystack_reference, "amount": TOTAL})[1]["data"]
    pid = str(dashboard["id"])
    fake_paystack.settle_refund(pid, "processed")
    assert refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=None, amount_kobo=TOTAL) == "review"
    assert get(db, refund).status == "needs_review"
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="made on the dashboard")
    assert out.status == "succeeded"
    [attempt] = attempts_of(db, refund)
    assert (attempt.paystack_refund_id, attempt.status) == (pid, "processed")  # reconcile's "taken" set sees it
    assert events(db, booking).count(("refund_processed", f"{refund.id}:complete")) == 1
    assert refunds.dispatch_due(db) == 0


def test_resolve_with_no_attempts_and_no_paystack_record_can_be_failed(db, fake_paystack):
    booking = make_booking(db, make_patient(db), total_amount_kobo=TOTAL, channel="app")
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=TOTAL, amount_kobo=TOTAL, destination="original_method", status="needs_review",
                    review_flagged_at=datetime.utcnow(), review_reason="odd", approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="nothing on the dashboard")
    assert out.status == "failed" and out.review_reason is None


def test_resolve_after_wallet_switch_keeps_the_credit_and_does_not_rerun_success(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    fake_paystack.settle_refund(pid, "processed")
    refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                               paystack_refund_id=pid, amount_kobo=TOTAL)
    assert get(db, refund).status == "needs_review"
    before = (events(db, booking), db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one().balance_kobo)
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="both paid; wallet stays")
    assert out.status == "succeeded" and out.review_flagged_at is None and out.review_reason is None
    assert (events(db, booking), get(db, db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one()).balance_kobo) == before
    assert db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 1


def test_resolve_failed_on_a_refund_whose_money_already_left_is_refused(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    fake_paystack.settle_refund(pid, "failed")  # Paystack says failed again, but the wallet was already credited
    refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                               paystack_refund_id=pid, amount_kobo=TOTAL)
    assert get(db, refund).status == "needs_review"
    with pytest.raises(refunds.RefundConflict) as e:
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="reverse it")
    assert "already" in e.value.message
    assert get(db, refund).status == "needs_review"
    assert db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one().balance_kobo == TOTAL


# --- review round 1 ---------------------------------------------------------------------------------------------------
def dashboard_refund(fake, booking, status, amount=100_000):
    """A partial refund made on the Paystack dashboard: on the transaction, tied to none of our attempts."""
    rec = fake._create_refund({"transaction": booking.paystack_reference, "amount": amount})[1]["data"]
    fake.refunds[str(rec["id"])]["status"] = status
    return str(rec["id"])


@pytest.mark.parametrize("status", ["pending", "processing", "processed"])
def test_an_untied_dashboard_refund_blocks_retry_switch_and_resolve_failed(db, fake_paystack, status):
    booking, refund, pid = failed(db, fake_paystack)
    dashboard_refund(fake_paystack, booking, status)  # a different amount: it must still block
    for act in (lambda: refunds.retry_refund(db, refund.id, admin=finance(db)),
                lambda: refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))):
        with pytest.raises(refunds.RefundConflict) as e:
            act()
        assert "another refund on this payment" in e.value.message
    assert get(db, refund).status == "failed" and db.query(PatientTransaction).filter_by(refund_id=refund.id).count() == 0
    b2, r2, p2 = reviewed(db, fake_paystack)
    dashboard_refund(fake_paystack, b2, status)
    fake_paystack.refunds[p2]["status"] = "failed"
    with pytest.raises(refunds.RefundConflict) as e:
        refunds.resolve_refund(db, r2.id, admin=super_admin(db), outcome="failed", note="checked")
    assert "another refund on this payment" in e.value.message


def test_an_untied_failed_dashboard_refund_does_not_block(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    dashboard_refund(fake_paystack, booking, "failed")
    assert refunds.retry_refund(db, refund.id, admin=finance(db)).status == "approved"


# --- bank details: separation of duties -----------------------------------------------------------------------------
def test_bank_details_not_by_the_approver_or_the_raiser(db, fake_paystack):
    booking, refund, pid = settled(db, fake_paystack, "needs-attention")
    approver, raiser, other = finance(db), finance(db), finance(db)
    r = get(db, refund)
    r.approved_by_admin_id, r.requested_by_admin_id = approver.id, raiser.id
    db.commit()
    for who in (approver, raiser):
        with pytest.raises(refunds.RefundForbidden) as e:
            refunds.submit_bank_details(db, refund.id, admin=who, bank_code=BANK, account_number=ACCOUNT)
        assert e.value.message == "Someone other than the person who approved this refund must enter the family's bank details."
    assert not [r for r in fake_paystack.requests if "retry_with_customer_details" in r[1]]
    assert refunds.submit_bank_details(db, refund.id, admin=other, bank_code=BANK, account_number=ACCOUNT).status == "processing"


def test_bank_details_above_the_finance_limit_need_a_super_admin(db, fake_paystack):
    booking, refund, pid = settled(db, fake_paystack, "needs-attention", total=10_000_000, amount=10_000_000)  # N100,000
    with pytest.raises(refunds.RefundForbidden):
        refunds.submit_bank_details(db, refund.id, admin=finance(db), bank_code=BANK, account_number=ACCOUNT)
    assert get(db, refund).status == "needs_attention"
    assert refunds.submit_bank_details(db, refund.id, admin=super_admin(db), bank_code=BANK,
                                       account_number=ACCOUNT).status == "processing"


def test_a_bank_details_refusal_logs_a_fixed_message(db, fake_paystack, caplog):
    booking, refund, pid = settled(db, fake_paystack, "needs-attention")
    fake_paystack.refunds[pid]["status"] = "processing"
    with pytest.raises(refunds.RefundConflict):
        refunds.submit_bank_details(db, refund.id, admin=finance(db), bank_code=BANK, account_number=ACCOUNT)
    assert "Refund is not in needs-attention status" not in caplog.text


# --- close asks Paystack; late processed on a closed refund -------------------------------------------------------------
def test_close_re_asks_paystack(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    with pytest.raises(refunds.RefundPaystackUnavailable):
        refunds.close_refund(db, refund.id, admin=finance(db), reason="stop")
    fake_paystack.refund_error = None
    fake_paystack.refunds[pid]["status"] = "processing"
    with pytest.raises(refunds.RefundConflict):
        refunds.close_refund(db, refund.id, admin=finance(db), reason="stop")
    fake_paystack.refunds[pid]["status"] = "processed"
    with pytest.raises(refunds.RefundConflict):
        refunds.close_refund(db, refund.id, admin=finance(db), reason="stop")
    assert get(db, refund).status == "needs_review"  # Paystack paid it: a person resolves


def test_processed_on_a_closed_refund_found_by_reference_goes_to_review(db, fake_paystack):
    booking, refund, pid = failed(db, fake_paystack)
    refunds.close_refund(db, refund.id, admin=finance(db), reason="stop")
    assert refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=None, amount_kobo=1) == "ignored"  # another amount: not ours
    assert refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=None, amount_kobo=TOTAL) == "review"
    assert get(db, refund).status == "needs_review"


# --- resolve: recorded truth ------------------------------------------------------------------------------------------
def flagged_closed(db, fake, **kw):
    """A closed refund that Paystack then processed while the booking has another open refund: flagged, still closed."""
    booking, refund, pid = failed(db, fake, **kw)
    refunds.close_refund(db, refund.id, admin=finance(db), reason="stop")
    again = refunds.request_refund(db, booking_id=booking.id, amount_kobo=100, reason_code="complaint", note=None,
                                   admin=finance(db))
    assert refunds.apply_refund_event(db, status="processed", transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=TOTAL) == "review"
    out = get(db, refund)
    assert out.status == "closed" and out.review_flagged_at is not None
    return booking, refund, pid, again


def test_resolve_succeeded_on_a_closed_refund_records_the_money_that_left(db, fake_paystack, caplog):
    booking, refund, pid, again = flagged_closed(db, fake_paystack, channel="whatsapp")
    fake_paystack.settle_refund(pid, "processed")
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="paid per dashboard")
    assert out.status == "succeeded" and out.processed_at is not None and out.review_flagged_at is None
    assert get(db, booking).payment_status == PaymentStatus.REFUNDED
    assert events(db, booking).count(("refund_processed", f"{refund.id}:complete")) == 1
    assert get(db, again).status == "pending_approval"


def test_resolve_succeeded_logs_an_error_when_refunds_exceed_the_total(db, fake_paystack, caplog):
    booking, refund, pid = reviewed(db, fake_paystack)
    db.add(Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                  suggested_amount_kobo=1000, amount_kobo=1000, destination="original_method", status="succeeded",
                  processed_at=datetime.utcnow()))
    db.commit()
    fake_paystack.settle_refund(pid, "processed")
    fake_paystack.refunds[pid]["amount"] = TOTAL
    with caplog.at_level("ERROR"):
        refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="succeeded", note="paid")
    assert "exceed" in caplog.text


def test_resolve_succeeded_with_the_amount_paystack_confirmed(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack, channel="whatsapp")  # Paystack processed TOTAL - 1
    fake_paystack.settle_refund(pid, "processed")
    fake_paystack.refunds[pid]["amount"] = TOTAL - 1
    sa = super_admin(db)
    with pytest.raises(refunds.RefundConflict):  # no amount given: still a mismatch
        refunds.resolve_refund(db, refund.id, admin=sa, outcome="succeeded", note="paid")
    with pytest.raises(refunds.RefundConflict):  # an amount that is not what Paystack confirmed
        refunds.resolve_refund(db, refund.id, admin=sa, outcome="succeeded", note="paid", amount_kobo=TOTAL)
    out = refunds.resolve_refund(db, refund.id, admin=sa, outcome="succeeded", note="paid", amount_kobo=TOTAL - 1)
    assert out.status == "succeeded" and out.amount_kobo == TOTAL - 1
    b = get(db, booking)
    assert b.payment_status == PaymentStatus.PARTIALLY_REFUNDED and b.refund_amount_kobo == TOTAL - 1


# --- Paystack has no record of our own attempt id -------------------------------------------------------------------
def test_not_found_on_our_attempt_refuses_retry_and_switch_accepts_resolve_failed_then_only_close(db, fake_paystack):
    booking, refund, pid = reviewed(db, fake_paystack)
    del fake_paystack.refunds[pid]  # Paystack answers 404 for the id we hold
    out = refunds.resolve_refund(db, refund.id, admin=super_admin(db), outcome="failed", note="not on the dashboard")
    assert out.status == "failed"
    with pytest.raises(refunds.RefundConflict):
        refunds.retry_refund(db, refund.id, admin=finance(db))
    with pytest.raises(refunds.RefundConflict):
        refunds.switch_to_wallet(db, refund.id, admin=super_admin(db))
    assert refunds.close_refund(db, refund.id, admin=finance(db), reason="no record").status == "closed"
