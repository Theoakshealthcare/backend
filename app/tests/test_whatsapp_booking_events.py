"""W2: the payment journey over time (reminder at 20 minutes, cancellation at 30) and the booking-event outbox."""

import threading
from datetime import datetime, timedelta

import pytest

from app.channels.whatsapp import notifications
from app.core.config import settings
from app.db.models.booking import BookingStatus, PaymentStatus
from app.db.models.whatsapp_booking import BookingEvent, Quote
from app.services import booking_events, dispatch
from app.services.booking_expiry import expire_unpaid_bookings
from app.services.booking_payment import settle_booking_payment
from app.tests.test_whatsapp_booking_chat import confirmed, start, the_booking  # noqa: F401
from app.tests.whatsapp_booking_helpers import (  # noqa: F401  (fixtures)
    Chat, onboarded,
)
from app.tests.factories import make_booking, make_patient


@pytest.fixture
def chat(db, fake_meta):
    return Chat(db, fake_meta)


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)


def sent_since(chat, before):
    return chat.fake.bodies[before:]


def text_of(body) -> str:
    interactive = body.get("interactive") or {}
    return body.get("text", {}).get("body") or interactive.get("body", {}).get("text") or ""


def link_sent_at(db, booking, minutes_ago):
    """Make the payment link look like it went out ``minutes_ago`` minutes ago (the booking's 30 minutes run from then)."""
    booking.payment_expires_at = datetime.utcnow() + timedelta(minutes=settings.PAYMENT_LINK_TTL_MINUTES - minutes_ago)
    db.commit()


# --- the reminder at 20 minutes --------------------------------------------------------------------------
def test_no_reminder_before_twenty_minutes(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    link_sent_at(db, the_booking(db), 19)
    assert notifications.scan_reminders(db, datetime.utcnow()) == 0


def test_a_reminder_is_queued_once_at_twenty_minutes_and_sent_as_free_text_with_pay_now(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    link_sent_at(db, booking, 20)
    before = len(chat.fake.bodies)
    now = datetime.utcnow()
    assert notifications.scan_reminders(db, now) == 1 and notifications.scan_reminders(db, now) == 0
    assert notifications.deliver_pending(db) == 1 and notifications.deliver_pending(db) == 0
    sent = sent_since(chat, before)
    assert len(sent) == 1 and sent[0]["type"] == "interactive"  # inside the 24 h window: no template needed
    assert text_of(sent[0]) == f"Your payment link for booking {booking.booking_code} expires in 10 minutes."
    assert sent[0]["interactive"]["action"]["parameters"]["display_text"] == "Pay now"
    assert sent[0]["interactive"]["action"]["parameters"]["url"].startswith("https://checkout.paystack.com/")


def test_the_reminder_says_how_long_is_really_left_from_the_settings(db, chat, client, booking_env, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_PAYMENT_REMINDER_MINUTES", 12)  # link lives 30: 18 minutes remain
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    link_sent_at(db, booking, 13)
    before = len(chat.fake.bodies)
    notifications.run_events(db)
    assert text_of(sent_since(chat, before)[0]) == f"Your payment link for booking {booking.booking_code} expires in 18 minutes."
    chat.contact.last_inbound_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    booking.payment_expires_at = datetime.utcnow() + timedelta(minutes=18)
    db.query(BookingEvent).delete()
    db.commit()
    before = len(chat.fake.bodies)
    notifications.run_events(db)
    template = sent_since(chat, before)[0]["template"]
    assert [p["text"] for p in template["components"][0]["parameters"]] == [booking.booking_code, "18"]


def test_outside_the_window_the_reminder_goes_as_the_payment_link_expiring_template(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    contact = chat.contact
    contact.last_inbound_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    link_sent_at(db, booking, 21)
    before = len(chat.fake.bodies)
    notifications.run_events(db)
    sent = sent_since(chat, before)
    assert len(sent) == 1 and sent[0]["type"] == "template"
    template = sent[0]["template"]
    assert template["name"] == "payment_link_expiring" and template["language"] == {"code": "en"}
    body, button = template["components"]
    assert body["parameters"] == [{"type": "text", "text": booking.booking_code}, {"type": "text", "text": "10"}]
    quote = db.query(Quote).filter_by(booking_id=booking.id).one()
    assert button == {"type": "button", "sub_type": "url", "index": "0", "parameters": [
        {"type": "text", "text": quote.payload["payment_url"].removeprefix("https://checkout.paystack.com/")}]}


def test_no_reminder_for_a_paid_or_cancelled_booking(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    link_sent_at(db, booking, 22)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    assert notifications.scan_reminders(db, datetime.utcnow()) == 0


def test_a_reminder_queued_just_before_payment_is_not_sent_stale(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    link_sent_at(db, booking, 22)
    notifications.scan_reminders(db, datetime.utcnow())
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)  # paid in the gap
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    texts = [text_of(b) for b in sent_since(chat, before)]
    assert not any("expires in 10 minutes" in t for t in texts)  # only the receipt goes out
    assert any("Payment of" in t for t in texts)


def test_only_whatsapp_bookings_get_reminders(db):
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING,
                           payment_expires_at=datetime.utcnow() + timedelta(minutes=5))
    assert notifications.scan_reminders(db, datetime.utcnow()) == 0 and booking.channel == "app"
    assert booking_events.emit(db, booking, "payment_received") is False  # app bookings are never queued


# --- the cancellation at 30 minutes (the existing expiry job) ------------------------------------------------
def test_an_unpaid_booking_is_cancelled_at_thirty_minutes_and_the_person_is_told_with_book_again(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert expire_unpaid_bookings(db) == 1
    booking = the_booking(db)
    assert (booking.status, booking.cancel_reason) == (BookingStatus.CANCELLED, "unpaid_timeout")
    before = len(chat.fake.bodies)
    notifications.run_events(db)
    sent = sent_since(chat, before)
    assert len(sent) == 1
    assert text_of(sent[0]) == f"Booking {booking.booking_code} was cancelled because payment wasn't completed."
    assert [b["reply"]["id"] for b in sent[0]["interactive"]["action"]["buttons"]] == ["menu:book"]
    assert sent[0]["interactive"]["action"]["buttons"][0]["reply"]["title"] == "Book again"
    assert chat.state == "idle" and chat.conversation.context.get("booking_id") is None


def test_outside_the_window_the_cancellation_is_the_template_with_a_quick_reply(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    chat.contact.last_inbound_at = datetime.utcnow() - timedelta(hours=30)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    expire_unpaid_bookings(db)
    before = len(chat.fake.bodies)
    notifications.run_events(db)
    sent = sent_since(chat, before)
    assert sent[0]["type"] == "template" and sent[0]["template"]["name"] == "booking_cancelled_unpaid"
    assert sent[0]["template"]["components"][1] == {"type": "button", "sub_type": "quick_reply", "index": "0",
                                                    "parameters": [{"type": "payload", "payload": "menu:book"}]}


def test_tapping_book_again_on_the_template_button_starts_a_booking(db, chat, client, booking_env):
    from app.tests.fake_meta import template_button_payload
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    expire_unpaid_bookings(db)
    notifications.run_events(db)  # the cancellation notice goes out and the chat is idle again
    chat.contact.last_inbound_at = datetime.utcnow() - timedelta(hours=30)
    db.commit()
    sent = chat.send(template_button_payload(chat.wa_id, "menu:book"))
    assert sent[0]["interactive"]["type"] == "flow" and chat.state == "booking_flow"


def test_a_booking_paid_after_the_cancel_and_reinstated_is_not_told_it_was_cancelled(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    expire_unpaid_bookings(db)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)  # the money arrived late; reinstated
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    texts = [text_of(b) for b in sent_since(chat, before)]
    assert texts == [f"Payment of ₦44,000 received for booking {booking.booking_code}. "
                     "We're assigning a professional now."]


# --- exactly once -------------------------------------------------------------------------------------------
def test_two_workers_delivering_at_once_send_one_message(db, chat, client, booking_env):
    from app.db.session import SessionLocal
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    before = len(chat.fake.bodies)
    barrier = threading.Barrier(4)

    def worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            notifications.deliver_pending(session)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(sent_since(chat, before)) == 1
    db.expire_all()
    assert db.query(BookingEvent).one().attempts == 1


def test_a_failed_send_is_retried_a_few_times_then_given_up(db, chat, client, booking_env):
    from app.tests.fake_meta import error_response
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    for _ in range(notifications.MAX_ATTEMPTS + 2):
        chat.fake.script(error_response(400, code=131026))
        notifications.deliver_pending(db, now=datetime.utcnow() + timedelta(hours=1, minutes=_ * 5))
    db.expire_all()
    event = db.query(BookingEvent).one()
    assert event.delivered_at is None and event.attempts == notifications.MAX_ATTEMPTS and event.last_error == "131026"


def test_a_claim_that_died_is_retried_after_the_lease(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    event = db.query(BookingEvent).one()
    event.claimed_at, event.attempts = datetime.utcnow(), 1  # another worker claimed it and died
    db.commit()
    before = len(chat.fake.bodies)
    assert notifications.deliver_pending(db) == 0  # still leased
    assert notifications.deliver_pending(db, now=datetime.utcnow() + timedelta(minutes=3)) == 1
    assert len(sent_since(chat, before)) == 1


def test_emit_is_idempotent(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    assert booking_events.emit(db, booking, "payment_link_expiring") is True
    assert booking_events.emit(db, booking, "payment_link_expiring") is False
    db.commit()
    assert db.query(BookingEvent).count() == 1
    with pytest.raises(ValueError):
        booking_events.emit(db, booking, "not_an_event")


# --- STOP, blocked, no contact ---------------------------------------------------------------------------
def test_a_person_who_sent_stop_gets_the_receipt_but_not_the_reminder_or_cancellation(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    chat.say("STOP")
    link_sent_at(db, booking, 22)
    notifications.run_events(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    expire_unpaid_bookings(db)
    before = len(chat.fake.bodies)
    notifications.run_events(db)
    assert sent_since(chat, before) == []
    db.expire_all()
    assert all(e.delivered_at is not None for e in db.query(BookingEvent))  # nothing left retrying


def test_a_paid_booking_receipt_still_reaches_someone_who_sent_stop(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    chat.say("STOP")
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    assert "Payment of ₦44,000 received" in text_of(sent_since(chat, before)[0])


def test_a_blocked_contact_gets_nothing(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    chat.contact.blocked = True
    db.commit()
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    assert sent_since(chat, before) == []


# --- the sweep ------------------------------------------------------------------------------------------------------
def test_the_sweep_closes_drafts_nobody_finished_and_wipes_their_answers(db, chat, client, booking_env):
    start(chat, client, booking_env)
    quote = db.query(Quote).one()
    assert quote.sensitive_encrypted
    quote.updated_at = datetime.utcnow() - timedelta(hours=2)
    db.commit()
    chat.conversation.updated_at = datetime.utcnow() - timedelta(hours=2)
    db.commit()
    assert notifications.sweep_stale(db, datetime.utcnow()) >= 1
    db.expire_all()
    quote = db.query(Quote).one()
    assert quote.status == "expired" and quote.sensitive_encrypted is None and "address" not in quote.payload
    assert chat.state == "idle"


def test_the_sweep_leaves_a_fresh_draft_and_a_created_booking_alone(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    notifications.sweep_stale(db, datetime.utcnow())
    db.expire_all()
    assert db.query(Quote).one().status == "confirmed" and the_booking(db).status == BookingStatus.PENDING
    assert chat.state == "awaiting_payment"


def test_the_sweep_wipes_answers_left_on_a_confirmed_quote_whose_link_never_came(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    quote = db.query(Quote).one()
    from app.core.security import encrypt
    quote.sensitive_encrypted, quote.updated_at = encrypt("{}"), datetime.utcnow() - timedelta(hours=2)
    db.commit()
    notifications.sweep_stale(db, datetime.utcnow())
    db.expire_all()
    assert db.query(Quote).one().sensitive_encrypted is None


# --- paying and cancelling at the same time (security review: money taken, person never told) -------------------
def test_cancel_asks_paystack_first_and_a_payment_that_already_landed_wins(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking_env.paystack.paid.add(booking.paystack_reference)  # they paid; the webhook has not arrived
    before = len(chat.fake.bodies)
    chat.say("cancel")
    booking = the_booking(db)
    assert booking_env.paystack.verified == [booking.paystack_reference]
    assert booking.payment_status == PaymentStatus.PAID and booking.status == BookingStatus.PENDING
    assert booking.cancel_reason is None and not booking.refund_required
    texts = [text_of(b) for b in sent_since(chat, before)]
    assert texts == [f"Payment of ₦44,000 received for booking {booking.booking_code}. "
                     "We're assigning a professional now."]
    assert chat.state == "idle"


def test_cancel_with_nothing_paid_still_cancels_after_one_check_with_paystack(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    chat.say("cancel")
    booking = the_booking(db)
    assert booking_env.paystack.verified == [booking.paystack_reference]
    assert (booking.status, booking.cancel_reason) == (BookingStatus.CANCELLED, "user_cancelled")
    assert chat.state == "idle"


def test_a_payment_that_does_not_match_the_booking_is_not_cancelled_over_the_persons_head(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking_env.paystack.paid.add(booking.paystack_reference)
    booking.total_amount_kobo += 100  # what Paystack took no longer equals what we expect
    db.commit()
    sent = chat.say("cancel")
    booking = the_booking(db)
    assert booking.status == BookingStatus.PENDING and booking.cancel_reason is None
    assert "Talk to a person" in text_of(sent[0]) and chat.state == "awaiting_payment"


def test_the_payment_is_settled_even_if_it_lands_between_the_check_and_the_cancel(db, chat, client, booking_env, monkeypatch):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    real_verify = booking_env.paystack.verify_transaction

    def paid_in_the_gap(service, reference):
        result = real_verify(service, reference)  # unpaid when Paystack is asked ...
        settle_booking_payment(db, booking.id, booking.total_amount_kobo)  # ... then the webhook wins the race
        return result

    monkeypatch.setattr("app.services.paystack.PaystackService.verify_transaction", paid_in_the_gap)
    chat.say("cancel")
    booking = the_booking(db)
    assert booking.payment_status == PaymentStatus.PAID and booking.status == BookingStatus.PENDING  # not cancelled
    assert not booking.refund_required


def test_a_booking_that_expired_but_was_paid_is_restored_when_the_person_comes_back(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    expire_unpaid_bookings(db)
    booking_env.paystack.paid.add(booking.paystack_reference)  # paid on the link; the webhook was lost
    before = len(chat.fake.bodies)
    chat.say("I paid")
    booking = the_booking(db)
    assert booking.payment_status == PaymentStatus.PAID and booking.status == BookingStatus.PENDING
    assert booking.cancel_reason is None and not booking.refund_required
    texts = [text_of(b) for b in sent_since(chat, before)]
    assert texts == [f"Payment of ₦44,000 received for booking {booking.booking_code}. "
                     "We're assigning a professional now."]
    assert chat.state == "idle"


def test_a_booking_that_expired_and_was_not_paid_is_still_reported_cancelled(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    expire_unpaid_bookings(db)
    sent = chat.say("hello?")
    assert booking_env.paystack.verified == [booking.paystack_reference]
    assert the_booking(db).status == BookingStatus.CANCELLED and "cancelled" in text_of(sent[0]).lower()


def test_the_cancellation_notice_is_not_sent_for_a_booking_paid_on_the_link_whose_webhook_was_lost(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    expire_unpaid_bookings(db)
    booking_env.paystack.paid.add(booking.paystack_reference)
    before = len(chat.fake.bodies)
    notifications.run_events(db)
    texts = [text_of(b) for b in sent_since(chat, before)]
    assert not any("was cancelled" in t for t in texts)
    assert the_booking(db).payment_status == PaymentStatus.PAID and the_booking(db).status == BookingStatus.PENDING


def test_paying_a_booking_that_was_already_cancelled_tells_the_person_a_refund_is_coming(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    chat.say("cancel")  # nothing paid yet, so it cancels; the Paystack link is still live
    booking = the_booking(db)
    outcome = settle_booking_payment(db, booking.id, booking.total_amount_kobo)  # they pay on it anyway
    assert outcome.value == "paid_not_dispatched"
    booking = the_booking(db)
    assert booking.refund_required and booking.refund_required_reason == "paid_after_cancellation"
    before = len(chat.fake.bodies)
    assert notifications.deliver_pending(db) == 1 and notifications.deliver_pending(db) == 0
    sent = sent_since(chat, before)
    assert text_of(sent[0]) == (f"We received your payment of ₦44,000 for booking {booking.booking_code}, but that "
                                "booking had already been cancelled. We'll refund you; our team will be in touch.")
    assert db.query(BookingEvent).filter_by(event="payment_refund_pending").one().delivered_at is not None


def test_the_refund_notice_goes_to_someone_who_sent_stop_and_as_a_template_outside_the_window(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    chat.say("cancel")
    booking = the_booking(db)
    chat.say("STOP")
    chat.contact.last_inbound_at = datetime.utcnow() - timedelta(hours=30)
    db.commit()
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    sent = sent_since(chat, before)
    assert len(sent) == 1 and sent[0]["type"] == "template" and sent[0]["template"]["name"] == "payment_refund_pending"
    body = sent[0]["template"]["components"][0]["parameters"]
    assert [p["text"] for p in body] == ["₦44,000", booking.booking_code]


def test_a_paid_expired_booking_too_close_to_the_visit_to_reinstate_also_tells_the_person_about_the_refund(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    booking.scheduled_date = datetime.utcnow() + timedelta(minutes=10)  # too soon to put back
    db.commit()
    expire_unpaid_bookings(db)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    texts = [text_of(b) for b in sent_since(chat, before)]
    assert len(texts) == 1 and "We'll refund you" in texts[0]  # and not "was cancelled because payment wasn't completed"
