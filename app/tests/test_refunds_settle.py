"""Refunds v2 task 5: the send job, the ``refund.*`` webhooks and the reconcile job.

The invariant: an approved refund is sent to Paystack at most once per attempt, and silence from Paystack never causes a resend
or a "failed". Paystack's word (a webhook, or reconcile) settles it; a contradiction goes to ``needs_review``."""
import threading
from datetime import datetime, timedelta

import pytest

from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.refund import Refund, RefundAttempt
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.services import refunds
from app.tests.factories import make_booking, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)
from app.tests.payout_helpers import WEBHOOK, signed
from app.tests.refund_helpers import assert_refund_conserved
from app.tests.whatsapp_notify_helpers import Chat, body_text, fake_meta, onboarded, wa_booking  # noqa: F401

TOTAL = 4_400_000


def approved(db, fake, *, amount=TOTAL, status="approved", total=TOTAL, **booking_fields):
    """A WhatsApp booking whose payment exists on the fake Paystack, with one refund heading for the original method."""
    booking_fields.setdefault("channel", "whatsapp")
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED, total_amount_kobo=total, **booking_fields)
    fake.initialize_transaction(None, "placeholder@example.com", total, booking.paystack_reference)
    fake.paid.add(booking.paystack_reference)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=amount, amount_kobo=amount, destination="original_method", status=status,
                    approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    return booking, refund


def reload(db, *objs):
    db.expire_all()
    return [db.get(type(o), o.id) for o in objs]


def attempts_of(db, refund):
    db.expire_all()
    return db.query(RefundAttempt).filter_by(refund_id=refund.id).order_by(RefundAttempt.attempt).all()


def posts(fake):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == "/refund"]


def events(db, booking):
    db.expire_all()
    return sorted((e.event, e.dedupe_key) for e in db.query(BookingEvent).filter_by(booking_id=booking.id))


def sent(db, fake, **kw):
    """An approved refund that has been sent: processing, attempt 1 pending at Paystack with its id."""
    booking, refund = approved(db, fake, **kw)
    assert refunds.dispatch_due(db) == 1
    attempt = attempts_of(db, refund)[0]
    return booking, refund, attempt.paystack_refund_id


def settle(client, fake, pid, status):
    return client.post(WEBHOOK, **signed(fake.settle_refund(pid, status)))


def apply(db, booking, status, pid=None, amount=None, **kw):
    return refunds.apply_refund_event(db, status=status, transaction_reference=booking.paystack_reference,
                                      paystack_refund_id=pid, amount_kobo=amount, **kw)


# --- sending -------------------------------------------------------------------------------------------------------
def test_send_claims_commits_then_calls_paystack(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    seen = {}
    real = fake_paystack.call

    def spy(method, path, params=None, json_body=None):
        if method == "POST" and path == "/refund":
            other = SessionLocal()  # another connection: it only sees what was COMMITTED before the call
            try:
                seen["attempts"] = [(a.attempt, a.status) for a in other.query(RefundAttempt).filter_by(refund_id=refund.id)]
                seen["status"] = other.get(Refund, refund.id).status
            finally:
                other.close()
        return real(method, path, params, json_body)

    fake_paystack.call = spy
    assert refunds.dispatch_due(db) == 1
    assert seen == {"attempts": [(1, "sending")], "status": "processing"}


def test_two_workers_send_once(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    real = fake_paystack.call
    inner = {}

    def spy(method, path, params=None, json_body=None):
        if method == "POST" and path == "/refund" and "second" not in inner:
            other = SessionLocal()  # a second worker runs while the first is mid-request
            try:
                inner["second"] = refunds.dispatch_due(other)
            finally:
                other.close()
        return real(method, path, params, json_body)

    fake_paystack.call = spy
    assert refunds.dispatch_due(db) == 1
    assert inner["second"] == 0
    assert len(posts(fake_paystack)) == 1
    assert len(attempts_of(db, refund)) == 1


def test_a_booking_locked_by_someone_else_is_skipped_not_waited_for(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    holder = SessionLocal()
    try:
        holder.query(Booking).filter_by(id=booking.id).with_for_update().one()
        assert refunds.dispatch_due(db) == 0
        assert posts(fake_paystack) == []
    finally:
        holder.rollback()
        holder.close()
    assert refunds.dispatch_due(db) == 1


def test_accepted_send_moves_to_processing_and_notifies_being_processed(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    r, = reload(db, refund)
    a = attempts_of(db, refund)[0]
    assert (r.status, a.status, a.attempt) == ("processing", "pending", 1)
    assert pid in fake_paystack.refunds and fake_paystack.refunds[pid]["amount"] == TOTAL
    assert events(db, booking) == [("refund_flagged", f"{refund.id}:processing")]
    assert fake_paystack.requests[-1][3] == {"transaction": booking.paystack_reference, "amount": TOTAL, "currency": "NGN"}


def test_refused_send_fails_with_paystack_message(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "422"
    assert refunds.dispatch_due(db) == 1
    r, = reload(db, refund)
    a = attempts_of(db, refund)[0]
    assert (r.status, a.status) == ("failed", "failed")
    assert r.failure_reason == "Refund could not be processed" == a.failure_reason
    assert events(db, booking) == []  # nothing to tell the family yet


def test_timeout_send_is_unknown_and_never_resent(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    for _ in range(3):
        refunds.dispatch_due(db)
    r, = reload(db, refund)
    assert (r.status, [a.status for a in attempts_of(db, refund)]) == ("processing", ["unknown"])
    assert len(posts(fake_paystack)) == 1
    assert_refund_conserved(db, booking)


def test_a_5xx_send_is_unknown_too(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "500"
    refunds.dispatch_due(db)
    assert [a.status for a in attempts_of(db, refund)] == ["unknown"]


def test_wallet_refunds_and_unapproved_refunds_are_not_sent(db, fake_paystack):
    approved(db, fake_paystack, status="pending_approval")
    booking, refund = approved(db, fake_paystack)
    refund.destination = "wallet"
    db.commit()
    assert refunds.dispatch_due(db) == 0
    assert posts(fake_paystack) == []


def test_a_retry_attempt_gets_the_next_number(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    apply(db, booking, "failed", pid, TOTAL, reason="Insufficient balance")
    r, = reload(db, refund)
    r.status = "approved"  # what Task 7's retry does
    db.commit()
    assert refunds.dispatch_due(db) == 1
    assert [(a.attempt, a.status) for a in attempts_of(db, refund)] == [(1, "failed"), (2, "pending")]


# --- webhooks ------------------------------------------------------------------------------------------------------
def test_processed_webhook_succeeds_once(db, client, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    body = fake_paystack.settle_refund(pid, "processed")
    assert client.post(WEBHOOK, **signed(body)).status_code == 200
    r, b = reload(db, refund, booking)
    assert (r.status, b.payment_status, b.refund_required) == ("succeeded", PaymentStatus.REFUNDED, False)
    assert r.processed_at is not None and attempts_of(db, refund)[0].status == "processed"
    assert client.post(WEBHOOK, **signed(body)).status_code == 200  # Paystack retries
    assert apply(db, booking, "processed", pid, TOTAL) == "duplicate"
    assert events(db, booking).count(("refund_processed", f"{refund.id}:complete")) == 1
    assert_refund_conserved(db, booking)


def test_partial_processed_sets_partially_refunded(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack, amount=1_000_000)
    assert apply(db, booking, "processed", pid, 1_000_000) == "applied"
    b, r = reload(db, booking, refund)
    assert (b.payment_status, b.refund_amount_kobo, r.status) == (PaymentStatus.PARTIALLY_REFUNDED, 1_000_000, "succeeded")


def test_needs_attention_webhook_notifies_bank_details(db, client, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    assert settle(client, fake_paystack, pid, "needs-attention").status_code == 200
    r, = reload(db, refund)
    assert (r.status, attempts_of(db, refund)[0].status) == ("needs_attention", "needs_attention")
    assert ("refund_needs_bank_details", f"{refund.id}:needs_attention") in events(db, booking)
    settle(client, fake_paystack, pid, "needs-attention")
    assert [e for e in events(db, booking) if e[0] == "refund_needs_bank_details"] == [
        ("refund_needs_bank_details", f"{refund.id}:needs_attention")]


def test_processing_after_needs_attention_returns_to_processing(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    apply(db, booking, "needs-attention", pid, TOTAL)
    assert apply(db, booking, "processing", pid, TOTAL) == "applied"
    r, = reload(db, refund)
    assert (r.status, attempts_of(db, refund)[0].status) == ("processing", "processing")


def test_pending_and_processing_events_only_move_forward(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    assert apply(db, booking, "processing", pid, TOTAL) == "applied"
    assert apply(db, booking, "pending", pid, TOTAL) == "duplicate"  # an older event arriving late
    assert attempts_of(db, refund)[0].status == "processing"


def test_failed_webhook_records_reason(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    assert apply(db, booking, "failed", pid, TOTAL, reason="Insufficient balance") == "applied"
    r, = reload(db, refund)
    a = attempts_of(db, refund)[0]
    assert (r.status, a.status, r.failure_reason, a.failure_reason) == (
        "failed", "failed", "Insufficient balance", "Insufficient balance")
    assert apply(db, booking, "failed", pid, TOTAL) == "duplicate"
    b, = reload(db, booking)
    assert b.payment_status == PaymentStatus.PAID  # nothing refunded


def test_failed_webhook_without_reason_has_a_default(db, client, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    settle(client, fake_paystack, pid, "failed")
    r, = reload(db, refund)
    assert r.status == "failed" and r.failure_reason


def test_webhook_matched_by_transaction_reference_when_refund_reference_null(db, client, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    refunds.dispatch_due(db)  # the reply was lost: the attempt is unknown and has no Paystack id
    fake_paystack.refund_error = None
    fake_paystack.refunds["1001"] = {"id": "1001", "transaction_reference": booking.paystack_reference,
                                     "amount": TOTAL, "status": "pending"}
    body = fake_paystack.settle_refund("1001", "processed")
    assert body["data"]["refund_reference"] is None
    assert client.post(WEBHOOK, **signed(body)).status_code == 200
    r, b = reload(db, refund, booking)
    assert (r.status, b.payment_status) == ("succeeded", PaymentStatus.REFUNDED)
    assert attempts_of(db, refund)[0].paystack_refund_id == "1001"


def test_amount_mismatch_goes_to_review(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    assert apply(db, booking, "processed", pid, TOTAL - 100) == "review"
    r, b = reload(db, refund, booking)
    assert (r.status, b.payment_status) == ("needs_review", PaymentStatus.PAID)
    assert refunds.dispatch_due(db) == 0  # and nothing is resent
    assert len(posts(fake_paystack)) == 1


def test_processed_for_old_attempt_goes_to_review(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    apply(db, booking, "failed", pid, TOTAL, reason="x")
    r, = reload(db, refund)
    r.status = "approved"
    db.commit()
    refunds.dispatch_due(db)  # attempt 2
    assert apply(db, booking, "processed", pid, TOTAL) == "review"  # attempt 1's id
    r, b = reload(db, refund, booking)
    assert (r.status, b.payment_status) == ("needs_review", PaymentStatus.PAID)


def test_processed_after_we_recorded_failed_goes_to_review(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    apply(db, booking, "failed", pid, TOTAL, reason="x")
    assert apply(db, booking, "processed", pid, TOTAL) == "review"
    r, = reload(db, refund)
    assert r.status == "needs_review"


def test_a_second_paystack_refund_for_one_attempt_goes_to_review(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    assert apply(db, booking, "processed", "9999", TOTAL) == "review"
    r, = reload(db, refund)
    assert r.status == "needs_review"


def test_refund_event_with_no_open_refund_is_ignored(db, fake_paystack):
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED)
    assert apply(db, booking, "processed", "999", TOTAL) == "ignored"
    assert refunds.apply_refund_event(db, status="processed", transaction_reference="NOPE", paystack_refund_id=None,
                                      amount_kobo=1) == "ignored"
    b, = reload(db, booking)
    assert b.payment_status == PaymentStatus.PAID


def test_unknown_status_is_ignored(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    assert apply(db, booking, "reversed", pid, TOTAL) == "ignored"


def test_webhook_endpoint_routes_refund_events(db, client, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    for status, expected in (("pending", "processing"), ("processing", "processing"), ("needs-attention", "needs_attention"),
                             ("processing", "processing"), ("processed", "succeeded")):
        assert settle(client, fake_paystack, pid, status).status_code == 200
        r, = reload(db, refund)
        assert r.status == expected, status


def test_webhook_failed_event_routes_and_unsigned_is_rejected(db, client, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    body = fake_paystack.settle_refund(pid, "failed")
    assert client.post(WEBHOOK, content=b"{}", headers={"x-paystack-signature": "bad"}).status_code == 400
    assert settle(client, fake_paystack, pid, "failed").status_code == 200
    r, = reload(db, refund)
    assert r.status == "failed"


def test_success_effects_roll_back_with_the_event(db, fake_paystack, monkeypatch):
    """If anything inside the success routine raises, the refund is not half-settled."""
    booking, refund, pid = sent(db, fake_paystack)
    monkeypatch.setattr(refunds, "_mark_succeeded", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        apply(db, booking, "processed", pid, TOTAL)
    r, = reload(db, refund)
    assert r.status == "processing" and attempts_of(db, refund)[0].status == "pending"


def test_concurrent_processed_webhooks_succeed_once(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    out = []

    def run():
        s = SessionLocal()
        try:
            out.append(apply(s, booking, "processed", pid, TOTAL))
        finally:
            s.close()

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out) == ["applied", "duplicate", "duplicate", "duplicate"]
    assert events(db, booking).count(("refund_processed", f"{refund.id}:complete")) == 1


# --- reconcile -----------------------------------------------------------------------------------------------------
def aged(db, refund, minutes):
    """Make the refund's attempts older by ``minutes``."""
    for a in attempts_of(db, refund):
        a.created_at = datetime.utcnow() - timedelta(minutes=minutes)
    db.commit()


def test_reconcile_settles_unknown_from_list_refunds(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    refunds.dispatch_due(db)
    fake_paystack.refund_error = None
    fake_paystack.refunds["1001"] = {"id": "1001", "transaction_reference": booking.paystack_reference,
                                     "amount": TOTAL, "status": "processed"}
    assert refunds.reconcile(db) == 0  # too recent: Paystack is not even asked
    assert [r for r in fake_paystack.requests if r[0] == "GET"] == []
    aged(db, refund, 11)
    assert refunds.reconcile(db) == 1
    r, b = reload(db, refund, booking)
    assert (r.status, b.payment_status) == ("succeeded", PaymentStatus.REFUNDED)
    assert attempts_of(db, refund)[0].paystack_refund_id == "1001"
    assert len(posts(fake_paystack)) == 1  # reconcile only reads


def test_reconcile_fetches_by_id_and_settles(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    fake_paystack.refunds[pid]["status"] = "failed"  # the webhook never came
    aged(db, refund, 11)
    assert refunds.reconcile(db) == 1
    r, = reload(db, refund)
    assert r.status == "failed"


def test_reconcile_leaves_a_still_processing_refund_alone(db, fake_paystack):
    booking, refund, pid = sent(db, fake_paystack)
    aged(db, refund, 11)
    assert refunds.reconcile(db) == 0
    r, = reload(db, refund)
    assert r.status == "processing"


def test_reconcile_no_record_after_grace_goes_to_review_never_failed(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    refunds.dispatch_due(db)
    fake_paystack.refund_error = None  # Paystack answers, and has nothing for this transaction
    aged(db, refund, 20)
    assert refunds.reconcile(db) == 0  # inside the grace: keep waiting
    r, = reload(db, refund)
    assert r.status == "processing"
    aged(db, refund, 31)
    assert refunds.reconcile(db) == 1
    r, = reload(db, refund)
    assert r.status == "needs_review" and "no record" in r.failure_reason.lower()
    assert [a.status for a in attempts_of(db, refund)] == ["unknown"]  # never "failed"
    assert refunds.dispatch_due(db) == 0 and refunds.reconcile(db) == 0
    assert len(posts(fake_paystack)) == 1


def test_reconcile_paystack_unreachable_changes_nothing(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    refunds.dispatch_due(db)
    aged(db, refund, 60)
    assert refunds.reconcile(db) == 0  # the list call also times out: that is not "no record"
    r, = reload(db, refund)
    assert r.status == "processing"


def test_reconcile_two_paystack_refunds_for_one_attempt_goes_to_review(db, fake_paystack):
    booking, refund = approved(db, fake_paystack)
    fake_paystack.refund_error = "timeout"
    refunds.dispatch_due(db)
    fake_paystack.refund_error = None
    for i in ("1001", "1002"):
        fake_paystack.refunds[i] = {"id": i, "transaction_reference": booking.paystack_reference, "amount": TOTAL,
                                    "status": "processed"}
    aged(db, refund, 11)
    assert refunds.reconcile(db) == 1
    r, b = reload(db, refund, booking)
    assert (r.status, b.payment_status) == ("needs_review", PaymentStatus.PAID)


def test_reconcile_ignores_an_earlier_refunds_paystack_record(db, fake_paystack):
    """A transaction can have several Paystack refunds; one already tied to an earlier attempt is not this one."""
    booking, refund, pid = sent(db, fake_paystack, amount=1_000_000)
    apply(db, booking, "processed", pid, 1_000_000)
    second = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=1_000_000, amount_kobo=1_000_000, destination="original_method",
                    status="approved", approved_at=datetime.utcnow())
    db.add(second)
    db.commit()
    fake_paystack.refund_error = "timeout"
    refunds.dispatch_due(db)
    fake_paystack.refund_error = None
    aged(db, second, 11)
    assert refunds.reconcile(db) == 0  # the only Paystack refund belongs to the first refund's attempt
    s, = reload(db, second)
    assert s.status == "processing"


# --- notifications ---------------------------------------------------------------------------------------------------
@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def deliver(db, chat):
    from app.channels.whatsapp import notifications
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    return chat.fake.bodies[before:]


def test_being_processed_notice_goes_out_for_a_staff_refund_with_the_refunds_amount(db, chat, fake_paystack):
    booking = wa_booking(db, chat)  # refund_required is False: a staff-raised refund never set it
    fake_paystack.initialize_transaction(None, "placeholder@example.com", booking.total_amount_kobo, booking.paystack_reference)
    fake_paystack.paid.add(booking.paystack_reference)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="complaint", origin="staff",
                    suggested_amount_kobo=1_000_000, amount_kobo=1_000_000, destination="original_method",
                    status="approved", approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    assert not booking.refund_required
    refunds.dispatch_due(db)
    bodies = deliver(db, chat)
    assert [body_text(b) for b in bodies] == [
        f"Your refund of ₦10,000 for booking {booking.booking_code} is being processed. Reply here if you have questions."]


def test_bank_details_notice_wording(db, chat, fake_paystack):
    booking = wa_booking(db, chat)
    fake_paystack.initialize_transaction(None, "placeholder@example.com", booking.total_amount_kobo, booking.paystack_reference)
    fake_paystack.paid.add(booking.paystack_reference)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="complaint", origin="staff",
                    suggested_amount_kobo=2_000_000, amount_kobo=2_000_000, destination="original_method",
                    status="approved", approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    refunds.dispatch_due(db)
    deliver(db, chat)
    pid = attempts_of(db, refund)[0].paystack_refund_id
    assert apply(db, booking, "needs-attention", pid, 2_000_000) == "applied"
    bodies = deliver(db, chat)
    assert [body_text(b) for b in bodies] == [
        f"Your refund of ₦20,000 for booking {booking.booking_code} is waiting for your bank details. "
        "Our team will contact you. Reply here if you have questions."]


def test_a_second_refund_on_the_same_booking_notifies_again(db, chat, fake_paystack):
    booking = wa_booking(db, chat)
    fake_paystack.initialize_transaction(None, "placeholder@example.com", booking.total_amount_kobo, booking.paystack_reference)
    fake_paystack.paid.add(booking.paystack_reference)
    amounts, texts = (1_000_000, 500_000), []
    for amount in amounts:
        refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                        suggested_amount_kobo=amount, amount_kobo=amount, destination="original_method",
                        status="approved", approved_at=datetime.utcnow())
        db.add(refund)
        db.commit()
        refunds.dispatch_due(db)
        pid = attempts_of(db, refund)[0].paystack_refund_id
        assert apply(db, booking, "processed", pid, amount) == "applied"
        texts += [body_text(b) for b in deliver(db, chat)]
    assert len(texts) == 2  # "being processed" is dropped once the refund is complete; "complete" goes out for each refund
    assert all("is complete" in t for t in texts) and "₦10,000" in texts[0] and "₦5,000" in texts[1]
