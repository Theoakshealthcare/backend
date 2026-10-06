"""W3: "My bookings" in chat: the list, a booking's detail, and cancelling it under the policy (WA-FR-38/39)."""

import json
import threading
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.whatsapp import WhatsAppConversation
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.tests.factories import make_booking, make_patient
from app.tests.factories_whatsapp import open_tickets
from app.tests.test_whatsapp_booking_chat import confirmed, dispatched, the_booking  # noqa: F401
from app.tests.whatsapp_booking_helpers import booking_env  # noqa: F401
from app.tests.fakes import expo  # noqa: F401  (fixture)
from app.tests.whatsapp_notify_helpers import (  # noqa: F401
    CONDITION, INSTRUCTIONS, Chat, accepted_booking, act, body_text, contact_patient, fake_meta, make_recipient,
    offer, onboarded, wa_booking,
)
from app.tests.factories import make_caregiver


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def row_ids(body):
    return [r["id"] for s in body["interactive"]["action"]["sections"] for r in s["rows"]]


def buttons(body):
    return [b["reply"]["id"] for b in body["interactive"]["action"]["buttons"]]


def confirm(chat, booking):
    """Ask, then tap the confirm button the bot offered (as a person does): the terms they saw are the ones sent."""
    ask = chat.tap(f"bk:cancel:{booking.id}")
    return chat.tap(buttons(ask[0])[0])


def reload(db, booking):
    db.expire_all()
    return db.get(Booking, booking.id)


# --- the list -------------------------------------------------------------------------------------------------
def test_my_bookings_lists_the_five_most_recent_with_code_service_date_and_status(db, chat):
    made = []
    for i in range(7):
        made.append(wa_booking(db, chat, created_at=datetime.utcnow() - timedelta(days=7 - i)))
    sent = chat.pick("menu:bookings")
    assert len(sent) == 1 and sent[0]["interactive"]["type"] == "list"
    assert row_ids(sent[0]) == [f"bk:view:{b.id}" for b in reversed(made[-5:])]
    row = sent[0]["interactive"]["action"]["sections"][0]["rows"][0]
    assert row["title"] == made[-1].booking_code
    assert row["description"].startswith("Nurse · ") and row["description"].endswith("Finding a professional")
    assert chat.state == "idle"


def test_the_list_covers_the_contact_their_recipients_and_their_own_app_bookings_but_nobody_elses(db, chat):
    mine = wa_booking(db, chat)
    recipient = make_recipient(db, chat.contact.user_id, "Folake Adeyemi")
    for_recipient = wa_booking(db, chat, recipient=recipient)
    app_booking = make_booking(db, contact_patient(db, chat))  # booked in the app by the same person
    stranger = make_booking(db, make_patient(db))
    other_chat = Chat(db, chat.fake)
    onboarded(other_chat)
    theirs = wa_booking(db, other_chat)
    sent = chat.pick("menu:bookings")
    ids = row_ids(sent[0])
    assert set(ids) == {f"bk:view:{b.id}" for b in (mine, for_recipient, app_booking)}
    assert f"bk:view:{stranger.id}" not in ids and f"bk:view:{theirs.id}" not in ids


def test_no_bookings_says_so_and_offers_the_menu(db, chat):
    sent = chat.pick("menu:bookings")
    assert "don't have any bookings yet" in body_text(sent[0]) and sent[0]["interactive"]["type"] == "list"


def test_a_contact_who_only_sees_a_recipients_booking_cannot_cancel_it(db, chat):
    other = Chat(db, chat.fake)
    onboarded(other)
    recipient = make_recipient(db, chat.contact.user_id, "Folake Adeyemi")
    booking = wa_booking(db, other, recipient=recipient)  # booked by someone else for the person this contact cares for
    detail = chat.tap(f"bk:view:{booking.id}")
    assert "Booking " + booking.booking_code in body_text(detail[0]) and "Folake" in body_text(detail[0])
    assert "bk:cancel" not in json.dumps(detail)
    forged = chat.tap(f"bk:cancel:{booking.id}")
    assert "Only the person who made this booking" in body_text(forged[0])
    forged = chat.tap(f"bk:confirm:{booking.id}")
    assert "Only the person who made this booking" in body_text(forged[0])
    assert reload(db, booking).status == BookingStatus.PENDING


# --- the detail -----------------------------------------------------------------------------------------------
def test_the_detail_shows_the_visit_code_and_caregiver_to_the_booker_and_no_health_data(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    sent = chat.tap(f"bk:view:{booking.id}")
    text = body_text(sent[0])
    code = decrypt(booking.visit_code_enc)
    assert f"Booking {booking.booking_code}" in text and "Professional: Ngozi" in text
    assert f"Visit code: {code}." in text and "Status: Professional assigned" in text and "Total: ₦44,000" in text
    assert "Okafor" not in text
    blob = json.dumps(sent)
    assert "SENTINEL" not in blob and booking.patient_address not in blob
    assert buttons(sent[0]) == [f"bk:cancel:{booking.id}", "bk:list"]


def test_the_visit_code_is_not_shown_to_someone_who_is_not_the_booker(db, chat):
    other = Chat(db, chat.fake)
    onboarded(other)
    recipient = make_recipient(db, chat.contact.user_id)
    booking, _ = accepted_booking(db, other, recipient=recipient)
    text = body_text(chat.tap(f"bk:view:{booking.id}")[0])
    assert "Visit code" not in text and decrypt(booking.visit_code_enc) not in text


@pytest.mark.parametrize("status, expected_buttons", [
    (BookingStatus.COMPLETED, ["menu:book", "bk:list"]),
    (BookingStatus.IN_PROGRESS, ["menu:human", "bk:list"]),
    (BookingStatus.CANCELLED, ["bk:list"]),
])
def test_actions_follow_the_status(db, chat, status, expected_buttons):
    booking = wa_booking(db, chat, status=status)
    assert buttons(chat.tap(f"bk:view:{booking.id}")[0]) == expected_buttons


def test_a_cancelled_booking_shows_its_refund(db, chat):
    booking = wa_booking(db, chat, status=BookingStatus.CANCELLED, refund_required=True, refund_amount_kobo=3_520_000)
    assert "Refund: ₦35,200, being processed." in body_text(chat.tap(f"bk:view:{booking.id}")[0])


def test_a_forged_or_unknown_id_reveals_nothing(db, chat):
    stranger = make_booking(db, make_patient(db), booking_code="STRANGER1")
    for tap in (f"bk:view:{stranger.id}", "bk:view:not-a-uuid", "bk:view:00000000-0000-0000-0000-000000000000"):
        sent = chat.tap(tap)
        assert "couldn't find that booking" in body_text(sent[0]) and "STRANGER1" not in json.dumps(sent)


# --- cancelling: the policy is shown, then applied ----------------------------------------------------------
def test_before_assignment_the_policy_is_a_full_refund_and_cancelling_flags_it(db, chat):
    booking = wa_booking(db, chat, hours_ahead=2)  # paid, nobody assigned yet, even close to the visit
    ask = chat.tap(f"bk:cancel:{booking.id}")
    assert body_text(ask[0]) == f"Cancel booking {booking.booking_code}? You'll get a full refund of ₦44,000."
    assert buttons(ask[0]) == [f"bk:confirm:{booking.id}:0", f"bk:keep:{booking.id}"]
    done = chat.tap(buttons(ask[0])[0])
    assert body_text(done[0]) == f"Booking {booking.booking_code} is cancelled. We've started your refund of ₦44,000."
    booking = reload(db, booking)
    assert booking.status == BookingStatus.CANCELLED and booking.cancel_reason == "cancelled_by_booker"
    assert (booking.refund_required, booking.refund_required_reason, booking.refund_amount_kobo) == (
        True, "cancelled_by_booker", 4_400_000)
    assert chat.state == "idle"


def test_assigned_and_more_than_twelve_hours_out_is_a_full_refund_and_the_caregiver_is_pushed(db, chat, expo):
    from app.db.models.push_token import PushToken
    from app.channels.whatsapp import notifications
    booking, caregiver = accepted_booking(db, chat, hours_ahead=30)
    db.add(PushToken(user_id=caregiver.user_id, token="ExponentPushToken[w3-cg]", platform="ios"))
    db.commit()
    ask = chat.tap(f"bk:cancel:{booking.id}")
    assert "full refund of ₦44,000" in body_text(ask[0])
    confirm(chat, booking)
    assert reload(db, booking).refund_amount_kobo == 4_400_000
    notifications.deliver_pending(db)
    assert [m["data"]["type"] for m in expo.messages] == ["booking_cancelled"]


def test_assigned_inside_twelve_hours_keeps_the_fee_and_says_so_before_and_after(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    fee = (booking.service_fee_kobo * 20 + 50) // 100
    ask = chat.tap(f"bk:cancel:{booking.id}")
    text = body_text(ask[0])
    assert "less than 12 hours" in text and "20% fee on the service fee applies" in text
    assert f"₦{fee // 100:,}" in text and f"₦{(booking.total_amount_kobo - fee) // 100:,}" in text
    done = confirm(chat, booking)
    assert "a fee of" in body_text(done[0])
    booking = reload(db, booking)
    assert booking.refund_amount_kobo == booking.total_amount_kobo - fee and booking.refund_required


def test_the_fee_and_window_follow_the_settings(db, chat, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_CANCEL_FEE_PERCENT", 50)
    monkeypatch.setattr(settings, "WHATSAPP_CANCEL_FREE_HOURS", 48)
    booking, _ = accepted_booking(db, chat, hours_ahead=30)
    fee = (booking.service_fee_kobo * 50 + 50) // 100
    confirm(chat, booking)
    assert reload(db, booking).refund_amount_kobo == booking.total_amount_kobo - fee


def test_a_zero_percent_fee_means_a_full_refund_even_when_close(db, chat, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_CANCEL_FEE_PERCENT", 0)
    booking, _ = accepted_booking(db, chat, hours_ahead=3)
    assert "full refund" in body_text(chat.tap(f"bk:cancel:{booking.id}")[0])
    confirm(chat, booking)
    assert reload(db, booking).refund_amount_kobo == booking.total_amount_kobo


def test_a_caregiver_already_on_the_way_is_always_inside_the_fee_window(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=40)
    act(db, booking, caregiver, "on_my_way")
    assert "fee" in body_text(chat.tap(f"bk:cancel:{booking.id}")[0])


def test_keep_booking_goes_back_to_the_detail_and_cancels_nothing(db, chat):
    booking = wa_booking(db, chat)
    chat.tap(f"bk:cancel:{booking.id}")
    sent = chat.tap(f"bk:keep:{booking.id}")
    assert f"Booking {booking.booking_code}" in body_text(sent[0])
    assert reload(db, booking).status == BookingStatus.PENDING


def test_a_visit_in_progress_is_handed_to_a_person_not_cancelled(db, chat):
    booking = wa_booking(db, chat, status=BookingStatus.IN_PROGRESS)
    for tap in ("bk:cancel", "bk:confirm"):
        sent = chat.tap(f"{tap}:{booking.id}")
        assert "already under way" in body_text(sent[0])
        assert chat.state == "human"
        chat.tap("menu:human")  # whatever happens next, the booking is untouched
        assert reload(db, booking).status == BookingStatus.IN_PROGRESS
        chat.conversation.state = "idle"
        db.commit()
    assert [t.reason for t in open_tickets(db)] == ["talk_to_person"]


@pytest.mark.parametrize("status", [BookingStatus.COMPLETED, BookingStatus.DECLINED])
def test_a_finished_booking_cannot_be_cancelled(db, chat, status):
    booking = wa_booking(db, chat, status=status)
    sent = chat.tap(f"bk:confirm:{booking.id}")
    assert "can't be cancelled" in body_text(sent[0])
    assert reload(db, booking).status == status and not reload(db, booking).refund_required


def test_confirming_twice_cancels_once_and_the_second_tap_says_so(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=30)
    button = buttons(chat.tap(f"bk:cancel:{booking.id}")[0])[0]
    first, second = chat.tap(button), chat.tap(button)  # a double tap on the same button
    assert "is cancelled" in body_text(first[0]) and "already cancelled" in body_text(second[0])
    assert db.query(BookingEvent).filter_by(event="caregiver_booking_cancelled").count() == 1


def test_cancelling_races_a_caregiver_starting_the_visit_and_exactly_one_wins(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=30)
    code = decrypt(booking.visit_code_enc)
    outcomes = {}
    barrier = threading.Barrier(2)

    def cancel():
        s = SessionLocal()
        try:
            from app.services.booking_cancellation import cancel_by_booker
            barrier.wait()
            outcomes["cancel"] = cancel_by_booker(s, booking.id).status
        finally:
            s.close()

    def start():
        s = SessionLocal()
        try:
            barrier.wait()
            try:
                act(s, booking, caregiver, "start", visit_code=code)
                outcomes["start"] = "ok"
            except Exception as exc:
                outcomes["start"] = type(exc).__name__
        finally:
            s.close()

    threads = [threading.Thread(target=cancel), threading.Thread(target=start)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    final = reload(db, booking).status
    if outcomes["cancel"] == "cancelled":
        assert final == BookingStatus.CANCELLED and outcomes["start"] != "ok"
    else:  # the visit started first: the booker is refused and nothing is flagged
        assert final == BookingStatus.IN_PROGRESS and outcomes["cancel"] == "refused"
        assert not reload(db, booking).refund_required


# --- an unpaid booking: W2's verify-before-cancel ------------------------------------------------------------------
def unpaid_booking(db, chat, client, booking_env):
    confirmed(chat, client, booking_env)
    booking = the_booking(db)
    conversation = db.query(WhatsAppConversation).filter_by(contact_id=chat.contact.id).one()
    conversation.state, conversation.context = "idle", {}  # as if they had wandered off to look at their bookings
    db.commit()
    return booking


def test_cancelling_an_unpaid_booking_asks_nothing_to_be_refunded(db, chat, client, booking_env, dispatched):
    booking = unpaid_booking(db, chat, client, booking_env)
    ask = chat.tap(f"bk:cancel:{booking.id}")
    assert body_text(ask[0]) == (f"Cancel booking {booking.booking_code}? You haven't paid yet, "
                                 "so nothing will be charged.")
    done = confirm(chat, booking)
    assert booking_env.paystack.verified == [booking.paystack_reference]  # Paystack was asked first
    assert "cancelled that booking. Nothing was charged" in body_text(done[0])
    booking = reload(db, booking)
    assert booking.status == BookingStatus.CANCELLED and not booking.refund_required
    assert chat.state == "idle" and dispatched == []


def test_an_unpaid_booking_that_was_paid_on_the_link_is_settled_not_cancelled(db, chat, client, booking_env, dispatched):
    booking = unpaid_booking(db, chat, client, booking_env)
    booking_env.paystack.paid.add(booking.paystack_reference)  # the money is at Paystack; our webhook is late
    confirm(chat, booking)
    booking = reload(db, booking)
    assert booking.payment_status == PaymentStatus.PAID and booking.status == BookingStatus.PENDING
    assert dispatched == [booking.id] and not booking.refund_required
    assert chat.state == "idle"


def test_when_paystack_cannot_be_asked_nothing_is_cancelled(db, chat, client, booking_env, monkeypatch):
    from app.services import booking_payment
    booking = unpaid_booking(db, chat, client, booking_env)
    monkeypatch.setattr(booking_payment, "verify_and_settle", lambda db, booking_id: booking_payment.SettleOutcome.UNREACHABLE)
    sent = confirm(chat, booking)
    assert "couldn't confirm your payment status" in body_text(sent[0])
    assert reload(db, booking).status == BookingStatus.PENDING and chat.state == "idle"


# --- the fee the person confirmed is the fee they pay ----------------------------------------------------------------
def test_the_confirm_button_carries_the_fee_that_was_shown(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    fee = (booking.service_fee_kobo * 20 + 50) // 100
    ask = chat.tap(f"bk:cancel:{booking.id}")
    assert buttons(ask[0]) == [f"bk:confirm:{booking.id}:{fee}", f"bk:keep:{booking.id}"]
    free = wa_booking(db, chat)
    assert buttons(chat.tap(f"bk:cancel:{free.id}")[0])[0] == f"bk:confirm:{free.id}:0"


def test_a_caregiver_accepting_between_the_ask_and_the_tap_does_not_cost_a_surprise_fee(db, chat):
    booking = wa_booking(db, chat, hours_ahead=6)  # nobody assigned: cancelling is free
    ask = chat.tap(f"bk:cancel:{booking.id}")
    confirm_id = buttons(ask[0])[0]
    assert confirm_id.endswith(":0")
    caregiver = make_caregiver(db)
    offer(db, booking, caregiver)
    act(db, booking, caregiver, "accept")  # now assigned and inside 12 h: a fee applies
    sent = chat.tap(confirm_id)
    text = body_text(sent[0])
    assert "terms have changed" in text and "20% fee" in text
    after = reload(db, booking)
    assert after.status == BookingStatus.ACCEPTED and not after.refund_required  # nothing was cancelled
    fee = (booking.service_fee_kobo * 20 + 50) // 100
    assert buttons(sent[0])[0] == f"bk:confirm:{booking.id}:{fee}"
    done = chat.tap(buttons(sent[0])[0])  # confirming the terms they have now seen
    assert "a fee of" in body_text(done[0]) and reload(db, booking).refund_amount_kobo == booking.total_amount_kobo - fee


def test_the_twelve_hour_boundary_passing_between_the_ask_and_the_tap_re_asks(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=12.5)
    ask = chat.tap(f"bk:cancel:{booking.id}")
    assert "full refund" in body_text(ask[0])
    db.query(Booking).filter_by(id=booking.id).update({"scheduled_date": datetime.utcnow() + timedelta(hours=11)})
    db.commit()
    sent = chat.tap(buttons(ask[0])[0])
    assert "terms have changed" in body_text(sent[0]) and "fee" in body_text(sent[0])
    assert reload(db, booking).status == BookingStatus.ACCEPTED


def test_a_lower_fee_than_shown_is_fine(db, chat, monkeypatch):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    ask = chat.tap(f"bk:cancel:{booking.id}")
    monkeypatch.setattr(settings, "WHATSAPP_CANCEL_FEE_PERCENT", 10)  # the fee dropped since it was shown
    done = chat.tap(buttons(ask[0])[0])
    fee = (booking.service_fee_kobo * 10 + 50) // 100
    assert reload(db, booking).refund_amount_kobo == booking.total_amount_kobo - fee


@pytest.mark.parametrize("suffix", ["", ":", ":abc", ":-5", ":1.5", ":0:0"])
def test_a_bare_or_malformed_confirm_button_never_cancels_it_asks_again(db, chat, suffix):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    sent = chat.tap(f"bk:confirm:{booking.id}{suffix}")
    assert reload(db, booking).status == BookingStatus.ACCEPTED and not reload(db, booking).refund_required
    assert "Cancel booking" in body_text(sent[0]) and "20% fee" in body_text(sent[0])
    assert buttons(sent[0])[0].startswith(f"bk:confirm:{booking.id}:")


def test_a_forged_zero_fee_cannot_skip_the_fee(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    sent = chat.tap(f"bk:confirm:{booking.id}:0")
    assert reload(db, booking).status == BookingStatus.ACCEPTED and "terms have changed" in body_text(sent[0])


def test_an_unpaid_booking_confirm_without_the_token_asks_first(db, chat, client, booking_env, dispatched):
    booking = unpaid_booking(db, chat, client, booking_env)
    sent = chat.tap(f"bk:confirm:{booking.id}")
    assert reload(db, booking).status == BookingStatus.PENDING and booking_env.paystack.verified == []
    assert "haven't paid yet" in body_text(sent[0])
