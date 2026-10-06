"""W2: booking in chat, end to end through the processor (consent, Flow, location, quote, booking, payment)."""

import json
import threading
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.care import CareRecipient
from app.db.models.support import SupportConversation
from app.db.models.whatsapp_booking import BookingEvent, Quote
from app.services import dispatch
from app.services.booking import BookingService
from app.services.booking_payment import settle_booking_payment
from app.services.pricing import compute_price
from app.schemas.booking import BookingPreviewRequest
from app.tests.fake_meta import location_payload
from app.tests.whatsapp_booking_helpers import (  # noqa: F401  (fixtures)
    CONDITION, INSTRUCTIONS, Chat, FlowDriver, all_text, flow_reply_payload, flow_token_from,
    onboarded,
)


@pytest.fixture
def chat(db, fake_meta):
    return Chat(db, fake_meta)


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: calls.append(booking_id))
    return calls


def start(chat, client, env, **fill):
    """Menu -> Book -> a filled-in Flow -> the completion message. Returns (driver, the bot's reply)."""
    onboarded(chat)
    chat.pick("menu:book")
    driver = FlowDriver(client, env.public, flow_token_from(chat.fake.bodies))
    status, body = driver.fill(**fill)
    assert body["screen"] == "SUCCESS", body
    return driver, chat.send(flow_reply_payload(chat.wa_id, driver.token))


def pin(chat, lat=6.4474, lng=3.4723):
    return chat.send(location_payload(chat.wa_id, lat, lng))


def quote_for(chat, client, env, **fill):
    driver, _ = start(chat, client, env, **fill)
    return pin(chat)


def confirmed(chat, client, env, **fill):
    quote_for(chat, client, env, **fill)
    return chat.tap("quote:confirm")


def the_booking(db) -> Booking:
    db.expire_all()
    return db.query(Booking).one()


def the_quote(db) -> Quote:
    db.expire_all()
    return db.query(Quote).order_by(Quote.created_at.desc()).first()


def reply_text(body) -> str:
    interactive = body.get("interactive") or {}
    return (body.get("text", {}).get("body") or interactive.get("body", {}).get("text") or "")


def buttons(body):
    return [b["reply"]["id"] for b in body["interactive"]["action"]["buttons"]]


# --- opening the Flow ---------------------------------------------------------------------------
def test_book_a_visit_sends_the_booking_flow_and_waits_for_it(db, chat, client, booking_env):
    onboarded(chat)
    sent = chat.pick("menu:book")
    assert chat.state == "booking_flow" and len(sent) == 1
    parameters = sent[0]["interactive"]["action"]["parameters"]
    assert sent[0]["interactive"]["type"] == "flow" and parameters["flow_id"] == "flow-test-1"
    assert parameters["flow_action"] == "data_exchange" and "flow_action_payload" not in parameters
    quote = the_quote(db)
    assert quote.status == "draft" and chat.conversation.context["quote_id"] == str(quote.id)


def test_the_doctor_home_visit_row_also_opens_the_flow(db, chat, client, booking_env):
    onboarded(chat)
    assert chat.pick("menu:doctor")[0]["interactive"]["type"] == "flow"


def test_without_a_flow_id_booking_still_hands_over_like_w1(db, chat, monkeypatch, booking_env):
    monkeypatch.setattr(settings, "WHATSAPP_BOOKING_FLOW_ID", "")
    onboarded(chat)
    sent = chat.pick("menu:book")
    assert chat.state == "human" and "coming soon" in sent[0]["text"]["body"].lower()


def test_a_contact_without_an_oakly_account_cannot_book_and_is_handed_over(db, chat, booking_env):
    from app.tests.factories import make_user
    make_user(db, phone_number="0" + chat.wa_id[3:])  # an app account already owns the number: no link yet
    onboarded(chat)
    assert chat.contact.user_id is None
    chat.pick("menu:book")
    assert chat.state == "human" and db.query(Quote).count() == 0


def test_starting_again_closes_the_earlier_open_draft(db, chat, client, booking_env):
    onboarded(chat)
    chat.pick("menu:book")
    first = the_quote(db).id
    chat.say("cancel")
    chat.pick("menu:book")
    db.expire_all()
    statuses = {q.id: q.status for q in db.query(Quote).all()}
    assert statuses[first] == "cancelled" and sorted(statuses.values()) == ["cancelled", "draft"]


def test_other_words_while_the_flow_is_open_remind_and_resend_it(db, chat, client, booking_env):
    onboarded(chat)
    chat.pick("menu:book")
    sent = chat.say("hello?")
    assert chat.state == "booking_flow" and sent[0]["interactive"]["type"] == "flow"
    assert "CANCEL" in reply_text(sent[0])


def test_cancel_while_the_flow_is_open_goes_back_to_the_menu(db, chat, client, booking_env):
    onboarded(chat)
    chat.pick("menu:book")
    sent = chat.say("cancel")
    assert chat.state == "idle" and sent[0]["interactive"]["type"] == "list"
    assert the_quote(db).status == "cancelled"


# --- the Flow is submitted: location --------------------------------------------------------------
def test_finishing_the_flow_asks_for_a_location_pin(db, chat, client, booking_env):
    driver, sent = start(chat, client, booking_env)
    assert chat.state == "awaiting_location" and len(sent) == 1
    assert sent[0]["interactive"]["type"] == "location_request_message"
    assert "SKIP" in reply_text(sent[0])


def test_a_stale_or_forged_completion_is_not_trusted(db, chat, client, booking_env):
    onboarded(chat)
    chat.pick("menu:book")
    sent = chat.send(flow_reply_payload(chat.wa_id, "forged.token.value"))
    assert chat.state == "idle" and sent[0]["interactive"]["type"] == "list"
    assert "expired" in reply_text(sent[0]).lower()


def test_finishing_a_flow_that_was_not_completed_asks_to_finish_it(db, chat, client, booking_env):
    onboarded(chat)
    chat.pick("menu:book")
    token = flow_token_from(chat.fake.bodies)
    sent = chat.send(flow_reply_payload(chat.wa_id, token))  # nothing was answered
    assert chat.state == "booking_flow" and sent[0]["interactive"]["type"] == "flow"


def test_another_contacts_token_is_not_accepted(db, client, fake_meta, booking_env):
    a, b = Chat(db, fake_meta), Chat(db, fake_meta)
    onboarded(a)
    a.pick("menu:book")
    token = flow_token_from(fake_meta.bodies)
    onboarded(b)
    b.pick("menu:book")
    sent = b.send(flow_reply_payload(b.wa_id, token))
    assert "expired" in reply_text(sent[0]).lower() or sent[0]["interactive"]["type"] == "flow"
    assert db.query(Quote).filter_by(status="submitted").count() == 0


def test_the_pin_is_stored_and_the_quote_is_sent(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    quote = the_quote(db)
    assert quote.status == "quoted" and chat.state == "quoted"
    assert (quote.payload["lat"], quote.payload["lng"]) == (6.4474, 3.4723)
    assert quote.payload["needs_address_check"] is False


def test_skipping_the_pin_uses_the_city_centre_and_flags_the_booking_for_a_person(db, chat, client, booking_env):
    start(chat, client, booking_env)
    sent = chat.say("skip")
    quote = the_quote(db)
    assert chat.state == "quoted" and quote.payload["needs_address_check"] is True
    assert (quote.payload["lat"], quote.payload["lng"]) == (6.5244, 3.3792)
    assert sent[0]["interactive"]["type"] == "button"


def test_a_pin_outside_nigeria_is_treated_as_no_pin(db, chat, client, booking_env):
    start(chat, client, booking_env)
    pin(chat, 51.5, -0.12)
    assert the_quote(db).payload["needs_address_check"] is True


def test_other_words_while_waiting_for_the_pin_ask_again(db, chat, client, booking_env):
    start(chat, client, booking_env)
    sent = chat.say("hmm")
    assert chat.state == "awaiting_location" and sent[0]["interactive"]["type"] == "location_request_message"
    assert chat.say("cancel")[0]["interactive"]["type"] == "list" and chat.state == "idle"


# --- the quote ----------------------------------------------------------------------------------------
def test_the_quote_is_itemised_in_naira_with_the_cancellation_line_and_three_buttons(db, chat, client, booking_env):
    sent = quote_for(chat, client, booking_env)
    body = reply_text(sent[0])
    assert buttons(sent[0]) == ["quote:confirm", "quote:change", "quote:cancel"]
    titles = [b["reply"]["title"] for b in sent[0]["interactive"]["action"]["buttons"]]
    assert titles == ["Confirm & pay", "Change", "Cancel"]
    assert "Nurse" in body and "4 h" not in body
    assert "Service fee" in body and "₦40,000" in body
    assert "Platform fee" in body and "₦4,000" in body
    assert "Total" in body and "₦44,000" in body
    assert f"Free cancellation until {settings.WHATSAPP_CANCEL_FREE_HOURS} hours" in body
    assert body.startswith("Here's your quote:")  # for yourself, no name


def test_a_quote_for_someone_else_uses_their_first_name_only(db, chat, client, booking_env):
    sent = quote_for(chat, client, booking_env, who="new")
    assert reply_text(sent[0]).startswith("Here's your quote for Folake:")
    assert "Adeyemi" not in reply_text(sent[0])


def test_the_quote_uses_the_same_price_as_the_app_preview(db, chat, client, booking_env):
    quote_for(chat, client, booking_env, service="physiotherapist", hours=3)
    preview = BookingService(db).calculate_booking_preview(
        BookingPreviewRequest(service_type="physiotherapist", duration_hours=3))
    quote = the_quote(db)
    assert quote.total_kobo == round(preview.total_amount * 100)
    expected = compute_price(base_fee_kobo=1_000_000, quantity=3, multiplier=1.2, platform_fee_percent=10)
    assert quote.total_kobo == expected.total_kobo and quote.payload["breakdown"]["service_fee_kobo"] == \
        expected.service_fee_kobo


def test_surge_and_discount_get_their_own_lines(db, chat, client, booking_env):
    booking_env.prices.surge_pricing, booking_env.prices.surge_multiplier = True, 1.5
    booking_env.prices.discount = 10.0
    db.commit()
    body = reply_text(quote_for(chat, client, booking_env)[0])
    assert "Busy-time pricing" in body and "+₦20,000" in body
    assert "Discount" in body and "-₦6,000" in body and "Base fee (4 h)" in body
    expected = compute_price(base_fee_kobo=1_000_000, quantity=4, multiplier=1.0, surge_multiplier=1.5,
                             discount_percent=10.0, platform_fee_percent=10.0)
    assert the_quote(db).total_kobo == expected.total_kobo


def test_the_quote_is_locked_for_thirty_minutes_and_remembers_its_settings_row(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    quote = the_quote(db)
    assert quote.settings_id == booking_env.prices.id
    remaining = quote.expires_at - datetime.utcnow()
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)


def test_a_price_change_after_the_quote_does_not_change_the_booking(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    locked = the_quote(db).total_kobo
    booking_env.prices.base_fee_kobo = 2_000_000  # the admin doubles the rate while the person is deciding
    db.commit()
    chat.tap("quote:confirm")
    booking = the_booking(db)
    assert booking.total_amount_kobo == locked == 4_400_000
    assert booking_env.paystack.initialised[0]["amount"] == locked  # and Paystack is asked for the locked price
    assert booking.service_fee_kobo + booking.platform_fee_kobo == locked


def test_after_a_price_change_a_new_quote_uses_the_new_price(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    booking_env.prices.base_fee_kobo = 2_000_000
    db.commit()
    chat.tap("quote:change")
    driver = FlowDriver(client, booking_env.public, flow_token_from(chat.fake.bodies))
    driver.fill()
    chat.send(flow_reply_payload(chat.wa_id, driver.token))
    sent = pin(chat)
    assert "₦88,000" in reply_text(sent[0])


def test_a_service_without_a_multiplier_is_refused_at_quote_time_and_a_person_follows_up(db, chat, client, booking_env):
    start(chat, client, booking_env)
    booking_env.prices.multipliers = {"caregiver": 0.8}  # the nurse rate is removed between Flow and pin
    db.commit()
    sent = pin(chat)
    assert chat.state == "human" and "can't price" in reply_text(sent[0])
    assert db.query(Booking).count() == 0 and the_quote(db).status == "cancelled"
    assert db.query(SupportConversation).filter_by(reason="booking_placeholder").count() == 1


def test_a_quote_for_a_stranger_is_refused_if_the_relationship_is_gone(db, chat, client, booking_env):
    quote_for(chat, client, booking_env, who="new")
    from app.db.models.care import CareRelationship
    db.query(CareRelationship).filter_by(role="family").delete()
    db.commit()
    chat.tap("quote:confirm")
    assert db.query(Booking).count() == 0 and chat.state == "idle"


def test_change_starts_a_new_form_and_cancels_nothing_that_was_charged(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    sent = chat.tap("quote:change")
    assert chat.state == "booking_flow" and sent[0]["interactive"]["type"] == "flow"
    assert db.query(Booking).count() == 0
    db.expire_all()
    assert sorted(q.status for q in db.query(Quote)) == ["cancelled", "draft"]


def test_cancel_leaves_nothing_behind(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    sent = chat.tap("quote:cancel")
    assert chat.state == "idle" and sent[0]["interactive"]["type"] == "list"
    quote = the_quote(db)
    assert quote.status == "cancelled" and quote.sensitive_encrypted is None and "address" not in quote.payload
    assert db.query(Booking).count() == 0


def test_other_words_while_a_quote_is_open_show_it_again(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    sent = chat.say("hmm what")
    assert chat.state == "quoted" and buttons(sent[0]) == ["quote:confirm", "quote:change", "quote:cancel"]
    assert "₦44,000" in reply_text(sent[0])


def test_confirming_an_expired_quote_makes_no_booking(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    quote = the_quote(db)
    quote.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    sent = chat.tap("quote:confirm")
    assert db.query(Booking).count() == 0 and chat.state == "idle"
    assert "expired" in reply_text(sent[0]).lower() and booking_env.paystack.initialised == []
    assert the_quote(db).status == "expired"


def test_showing_an_expired_quote_again_expires_it_instead(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    quote = the_quote(db)
    quote.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    chat.say("what?")
    assert chat.state == "idle" and the_quote(db).status == "expired"


# --- confirm and pay -------------------------------------------------------------------------------
def test_confirm_creates_the_booking_and_sends_the_pay_now_link(db, chat, client, booking_env):
    sent = confirmed(chat, client, booking_env)
    booking = the_booking(db)
    contact = chat.contact
    assert (booking.channel, booking.booked_by_user_id) == ("whatsapp", contact.user_id)
    assert (booking.status, booking.payment_status) == (BookingStatus.PENDING, PaymentStatus.PENDING)
    assert booking.service_type == "nurse" and booking.duration_hours == 4 and booking.booking_for == "myself"
    assert booking.patient_city == "Lagos" and booking.patient_address == "12 Test Close, Lekki Phase 1"
    assert (booking.patient_latitude, booking.patient_longitude) == (6.4474, 3.4723)
    assert booking.needs_address_check is False
    recipient = db.get(CareRecipient, booking.care_recipient_id)
    assert recipient.created_by_user_id == contact.user_id  # "myself": the contact's own recipient row
    window = booking.payment_expires_at - datetime.utcnow()
    assert timedelta(minutes=29) < window <= timedelta(minutes=30)
    assert chat.state == "awaiting_payment" and chat.conversation.context["booking_id"] == str(booking.id)
    cta = sent[-1]["interactive"]
    assert cta["type"] == "cta_url" and cta["action"]["parameters"]["display_text"] == "Pay now"
    assert cta["action"]["parameters"]["url"].startswith("https://checkout.paystack.com/")
    assert "30 minutes" in cta["body"]["text"]


def test_paystack_is_asked_for_the_locked_kobo_with_the_internal_email_and_the_booking_reference(
        db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    init = booking_env.paystack.initialised
    assert len(init) == 1
    assert init[0]["amount"] == booking.total_amount_kobo == 4_400_000
    assert init[0]["reference"] == booking.paystack_reference
    assert init[0]["email"].startswith("wa-") and init[0]["email"].endswith("@pay.oaklycare.com")
    assert chat.wa_id not in init[0]["email"]  # the number itself never goes to Paystack in the clear
    assert init[0]["callback_url"].startswith(settings.WHATSAPP_PAYMENT_RETURN_URL)
    assert init[0]["metadata"]["booking_id"] == str(booking.id)


def test_the_condition_is_encrypted_on_the_booking_and_the_plain_column_stays_empty(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    assert booking.condition is None
    assert "SENTINEL" not in booking.condition_encrypted and decrypt(booking.condition_encrypted) == CONDITION
    assert booking.special_instructions == INSTRUCTIONS
    quote = the_quote(db)
    assert quote.status == "confirmed" and quote.booking_id == booking.id and quote.sensitive_encrypted is None


def test_a_booking_for_someone_else_points_at_their_recipient_row(db, chat, client, booking_env):
    confirmed(chat, client, booking_env, who="new")
    booking = the_booking(db)
    recipient = db.get(CareRecipient, booking.care_recipient_id)
    assert recipient.full_name == "Folake Adeyemi" and booking.booking_for == "parent"
    assert booking.patient_id != recipient.id  # the patient is the booker's account; the recipient is who is visited


def test_a_second_confirm_tap_makes_no_second_booking_and_resends_the_link(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    sent = chat.tap("quote:confirm")
    assert db.query(Booking).count() == 1 and len(booking_env.paystack.initialised) == 1
    assert sent[-1]["interactive"]["type"] == "cta_url"  # the same link, again
    assert chat.state == "awaiting_payment"


def test_a_replayed_confirm_after_a_dropped_reply_is_still_one_booking(db, chat, client, booking_env):
    """Same Meta message id delivered twice (Meta retries) is already deduped; a *different* id for the same intent
    (the user taps twice) must still not double-book."""
    confirmed(chat, client, booking_env)
    for _ in range(3):
        chat.tap("quote:confirm")
    assert db.query(Booking).count() == 1 and len(booking_env.paystack.initialised) == 1


def test_two_workers_confirming_at_once_create_exactly_one_booking(db, chat, client, booking_env):
    """The race the locks exist for: two taps handled by two workers at the same moment."""
    from app.channels.whatsapp.ingest import ingest, parse_webhook
    from app.channels.whatsapp.processor import process_inbound
    from app.db.session import SessionLocal
    from app.tests.fake_meta import button_payload

    quote_for(chat, client, booking_env)
    jobs = []
    for _ in range(2):
        jobs += ingest(db, parse_webhook(button_payload(chat.wa_id, "quote:confirm"))).jobs
    assert len(jobs) == 2
    barrier, errors = threading.Barrier(2), []

    def worker(job):
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            process_inbound(session, job)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(job,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    db.expire_all()
    assert db.query(Booking).count() == 1
    assert len(booking_env.paystack.initialised) == 1
    assert db.query(Quote).filter_by(status="confirmed").count() == 1
    assert chat.state == "awaiting_payment"


def test_a_failed_payment_link_leaves_no_payable_booking_and_goes_back_to_the_quote(db, chat, client, booking_env):
    booking_env.paystack.fail_init = "Paystack is having a moment"
    quote_for(chat, client, booking_env)
    sent = chat.tap("quote:confirm")
    booking = the_booking(db)
    assert booking.status == BookingStatus.CANCELLED and booking.payment_status == PaymentStatus.FAILED
    assert chat.state == "quoted" and the_quote(db).status == "quoted" and the_quote(db).booking_id is None
    assert "couldn't create your payment link" in reply_text(sent[0])
    assert buttons(sent[-1]) == ["quote:confirm", "quote:change", "quote:cancel"]  # the same locked quote, again
    # and the person can simply try again
    booking_env.paystack.fail_init = None
    sent = chat.tap("quote:confirm")
    assert sent[-1]["interactive"]["type"] == "cta_url" and chat.state == "awaiting_payment"
    active = db.query(Booking).filter_by(status=BookingStatus.PENDING).all()
    assert len(active) == 1 and db.query(Booking).count() == 2


def test_cancelling_before_paying_cancels_the_booking(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    sent = chat.say("cancel")
    booking = the_booking(db)
    assert (booking.status, booking.cancel_reason) == (BookingStatus.CANCELLED, "user_cancelled")
    assert chat.state == "idle" and "cancelled" in reply_text(sent[0]).lower()


def test_cancelling_a_paid_booking_here_is_refused(db, chat, client, booking_env, dispatched):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    sent = chat.say("cancel")
    assert the_booking(db).status == BookingStatus.PENDING and "already paid" in reply_text(sent[0])


# --- payment ----------------------------------------------------------------------------------------------
def test_the_webhook_settles_dispatches_and_the_person_is_told_once(db, chat, client, booking_env, dispatched):
    from app.channels.whatsapp.notifications import deliver_pending
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    outcome = settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    assert outcome.value == "paid" and dispatched == [booking.id]
    before = len(chat.fake.bodies)
    assert deliver_pending(db) == 1 and deliver_pending(db) == 0  # exactly one message, however often it runs
    sent = chat.fake.bodies[before:]
    assert reply_text(sent[0]) == f"Payment of ₦44,000 received for booking {booking.booking_code}. " \
                                  "We're assigning a professional now."
    assert chat.state == "idle" and chat.conversation.context.get("booking_id") is None
    event = db.query(BookingEvent).filter_by(event="payment_received").one()
    assert event.delivered_at is not None and event.attempts == 1


def test_the_paystack_webhook_endpoint_end_to_end(db, chat, client, booking_env, dispatched):
    import hashlib
    import hmac
    from app.channels.whatsapp.notifications import deliver_pending
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    body = json.dumps({"event": "charge.success", "data": {
        "reference": booking.paystack_reference, "amount": booking.total_amount_kobo, "status": "success",
        "customer": {"email": "x@y.z"}}}).encode()
    signature = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    assert client.post("/api/v1/payment/paystack/webhook", content=body,
                       headers={"x-paystack-signature": signature}).status_code == 200
    assert the_booking(db).payment_status == PaymentStatus.PAID and dispatched == [booking.id]
    assert deliver_pending(db) == 1


def test_a_payment_of_the_wrong_amount_is_not_confirmed_to_the_person(db, chat, client, booking_env, dispatched):
    from app.channels.whatsapp.notifications import deliver_pending
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    settle_booking_payment(db, booking.id, 100)
    assert the_booking(db).payment_status == PaymentStatus.PENDING and dispatched == []
    assert deliver_pending(db) == 0 and db.query(BookingEvent).count() == 0


def test_returning_before_the_webhook_verifies_with_paystack_and_confirms_once(db, chat, client, booking_env, dispatched):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    booking_env.paystack.paid.add(booking.paystack_reference)  # Paystack has the money; our webhook has not come
    before = len(chat.fake.bodies)
    chat.say("I have paid")
    sent = chat.fake.bodies[before:]
    assert booking_env.paystack.verified == [booking.paystack_reference]
    assert the_booking(db).payment_status == PaymentStatus.PAID and dispatched == [booking.id]
    assert len(sent) == 1 and "Payment of ₦44,000 received" in reply_text(sent[0])
    assert chat.state == "idle"
    # the real webhook arrives afterwards: nothing is repeated
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    from app.channels.whatsapp.notifications import deliver_pending
    assert deliver_pending(db) == 0 and dispatched == [booking.id]


def test_returning_when_nothing_is_paid_yet_resends_the_link_and_changes_nothing(db, chat, client, booking_env, dispatched):
    confirmed(chat, client, booking_env)
    sent = chat.say("did it work?")
    assert booking_env.paystack.verified and the_booking(db).payment_status == PaymentStatus.PENDING
    assert sent[-1]["interactive"]["type"] == "cta_url" and "haven't received" in reply_text(sent[-1])
    assert chat.state == "awaiting_payment" and dispatched == []


def test_a_paid_booking_with_no_pin_is_held_for_an_address_check_not_dispatched(db, chat, client, booking_env, dispatched):
    start(chat, client, booking_env)
    chat.say("skip")
    chat.tap("quote:confirm")
    booking = the_booking(db)
    assert booking.needs_address_check is True
    assert (booking.patient_latitude, booking.patient_longitude) == (6.5244, 3.3792)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    booking = the_booking(db)
    assert booking.payment_status == PaymentStatus.PAID and booking.dispatch_state is None and dispatched == []


# --- idle expiry --------------------------------------------------------------------------------------------
def test_an_unfinished_booking_starts_fresh_after_thirty_idle_minutes(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    conversation = chat.conversation
    conversation.updated_at = datetime.utcnow() - timedelta(minutes=31)
    db.commit()
    sent = chat.say("what now")
    assert chat.state == "idle" and sent[0]["interactive"]["type"] == "list"
    assert "been a while" in reply_text(sent[0])
    quote = the_quote(db)
    assert quote.status == "expired" and quote.sensitive_encrypted is None and "address" not in quote.payload
    assert chat.conversation.context.get("misses", 0) == 0  # a reset is not a miss


def test_a_stale_confirm_tap_after_the_idle_reset_makes_no_booking(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    chat.conversation.updated_at = datetime.utcnow() - timedelta(minutes=45)
    db.commit()
    chat.tap("quote:confirm")
    assert db.query(Booking).count() == 0 and chat.state == "idle"


def test_idle_expiry_never_touches_a_booking_that_was_created(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    chat.conversation.updated_at = datetime.utcnow() - timedelta(hours=3)
    db.commit()
    chat.say("hello")
    assert the_booking(db).id == booking.id and the_booking(db).status == BookingStatus.PENDING
    assert chat.state == "awaiting_payment" and chat.conversation.context["booking_id"] == str(booking.id)


def test_a_message_within_the_limit_keeps_the_booking_going(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    chat.conversation.updated_at = datetime.utcnow() - timedelta(minutes=20)
    db.commit()
    assert chat.tap("quote:confirm")[-1]["interactive"]["type"] == "cta_url"


# --- the service area --------------------------------------------------------------------------------
def test_outside_lagos_and_abuja_the_person_is_offered_the_waitlist(db, chat, client, booking_env):
    driver, sent = start(chat, client, booking_env, city="Port Harcourt")
    assert chat.state == "idle" and buttons(sent[0]) == ["waitlist:yes", "waitlist:no"]
    assert "Port Harcourt" in reply_text(sent[0]) and "Lagos and Abuja" in reply_text(sent[0])
    assert db.query(Booking).count() == 0


def test_joining_the_waitlist_keeps_only_the_city_and_service(db, chat, client, booking_env):
    start(chat, client, booking_env, city="other")
    sent = chat.tap("waitlist:yes")
    quote = the_quote(db)
    assert quote.status == "waitlist" and set(quote.payload) <= {"service", "city"}
    assert "added you" in reply_text(sent[0]) and chat.state == "idle"


def test_declining_the_waitlist_closes_it(db, chat, client, booking_env):
    start(chat, client, booking_env, city="other")
    sent = chat.tap("waitlist:no")
    assert the_quote(db).status == "cancelled" and sent[0]["interactive"]["type"] == "list"


# --- privacy ---------------------------------------------------------------------------------------------------------
def test_no_health_data_in_any_message_the_bot_sends(db, chat, client, booking_env, dispatched):
    from app.channels.whatsapp.notifications import deliver_pending
    confirmed(chat, client, booking_env, who="new")
    booking = the_booking(db)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    deliver_pending(db)
    everything = all_text(chat.fake.bodies).lower()
    for needle in ("sentinel", "wound", "surgery", "dressing", "gate code", "condition"):
        assert needle not in everything, needle
    assert "adeyemi" not in everything  # a surname never appears: first names only


def test_no_condition_text_in_any_whatsapp_table_or_the_logs(db, chat, client, booking_env, caplog, dispatched):
    import logging
    from sqlalchemy import text as sql
    caplog.set_level(logging.DEBUG)
    confirmed(chat, client, booking_env)
    dump = ""
    for table in ("whatsapp_contacts", "whatsapp_conversations", "whatsapp_messages", "quotes", "booking_events",
                  "support_conversations", "care_recipients", "consents"):
        dump += str(db.execute(sql(f"SELECT t::text FROM {table} t")).fetchall())
    assert "SENTINEL-CONDITION" not in dump and "SENTINEL-CONDITION" not in caplog.text
    booking_row = db.execute(sql("SELECT b::text FROM bookings b")).fetchall()
    assert "SENTINEL-CONDITION" not in str(booking_row)  # encrypted at rest on the booking too


def test_the_phone_number_is_not_in_the_quote_or_event_rows(db, chat, client, booking_env):
    from sqlalchemy import text as sql
    confirmed(chat, client, booking_env)
    for table in ("quotes", "booking_events"):
        assert chat.wa_id not in str(db.execute(sql(f"SELECT t::text FROM {table} t")).fetchall())


# --- safety nets still work mid-booking ------------------------------------------------------------------------
def test_an_emergency_in_the_middle_of_a_booking_still_gets_the_emergency_reply(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    sent = chat.say("my mother cannot breathe")
    assert chat.state == "emergency" and "112" in reply_text(sent[0])
    assert chat.say("hello?") == []  # a person owns it now


def test_stop_data_in_the_middle_of_a_booking_wipes_the_draft(db, chat, client, booking_env):
    start(chat, client, booking_env)
    chat.say("STOP DATA")
    quote = the_quote(db)
    assert quote.status == "cancelled" and quote.sensitive_encrypted is None and "address" not in quote.payload
    assert chat.state == "declined"


def test_a_person_can_ask_for_a_human_at_any_booking_step(db, chat, client, booking_env):
    quote_for(chat, client, booking_env)
    chat.say("talk to a person")
    assert chat.state == "human"
