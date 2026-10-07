"""W3: the visit's journey reaches the booker on WhatsApp and the caregiver by push, from the outbox."""

import json
import logging
from datetime import datetime, timedelta

import pytest

from app.channels.whatsapp import notifications
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus
from app.db.models.push_token import PushToken
from app.db.models.whatsapp_booking import BookingEvent
from app.services.notification_service import NotificationService
from app.tests.factories import make_booking, make_caregiver, make_patient
from app.tests.fakes import expo  # noqa: F401  (fixture)
from app.tests.whatsapp_notify_helpers import (  # noqa: F401
    CONDITION, INSTRUCTIONS, Chat, accepted_booking, act, body_text, events_of, fake_meta, issuance, offer, onboarded,
    template_params, wa_booking,
)


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def deliver(db, chat):
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    return chat.fake.bodies[before:]


def leave_window(db, chat):
    contact = chat.contact
    contact.last_inbound_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()


# --- recording: same transaction as the transition, nothing sent inline ----------------------------------
def test_accepting_records_who_is_coming_and_the_visit_code_without_sending(db, chat):
    before = len(chat.fake.bodies)
    booking, caregiver = accepted_booking(db, chat)
    key = issuance(booking, caregiver)
    assert events_of(db, booking) == sorted([("caregiver_assigned", key), ("visit_code_issued", key)])
    assert len(chat.fake.bodies) == before  # the transition sends nothing itself


def test_a_failed_transition_records_no_event(db, chat):
    caregiver = make_caregiver(db)
    booking = wa_booking(db, chat)  # never offered to this caregiver: the accept is refused
    with pytest.raises(Exception):
        act(db, booking, caregiver, "accept")
    assert events_of(db, booking) == []


def test_an_app_booking_records_no_booker_events(db, chat):
    patient = make_patient(db)
    caregiver = make_caregiver(db)
    booking = make_booking(db, patient)  # channel app
    offer(db, booking, caregiver)
    act(db, booking, caregiver, "accept")
    assert events_of(db, booking) == []


def test_every_step_of_the_journey_is_recorded_once(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    code = decrypt(booking.visit_code_enc)
    act(db, booking, caregiver, "on_my_way")
    act(db, booking, caregiver, "start", visit_code=code)
    act(db, booking, caregiver, "complete")
    names = [e for e, _ in events_of(db, booking)]
    assert sorted(names) == sorted(["caregiver_assigned", "visit_code_issued", "caregiver_en_route", "visit_started",
                                    "visit_completed"])


# --- delivery ----------------------------------------------------------------------------------------------
def test_assignment_is_sent_with_first_name_date_time_and_experience_and_no_health_data(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    sent = deliver(db, chat)
    texts = [body_text(b) for b in sent]
    assigned = next(t for t in texts if "will visit" in t)
    assert assigned.startswith(f"Booking {booking.booking_code}: Ngozi will visit on ")
    assert "Experience: 6 years." in assigned and "Okafor" not in assigned
    blob = json.dumps(sent)
    assert "SENTINEL" not in blob and CONDITION not in blob and INSTRUCTIONS not in blob
    assert booking.patient_address not in blob


def test_the_visit_code_reaches_the_booker_and_matches_the_booking(db, chat):
    booking, _ = accepted_booking(db, chat)
    code = decrypt(booking.visit_code_enc)
    sent = deliver(db, chat)
    message = next(body_text(b) for b in sent if "visit code" in body_text(b))
    assert message == (f"Your visit code for booking {booking.booking_code} is {code}. "
                       "Give it to the professional when they arrive, and to no one else.")


def test_outside_the_window_the_code_goes_as_the_approved_template(db, chat):
    booking, _ = accepted_booking(db, chat)
    code = decrypt(booking.visit_code_enc)
    leave_window(db, chat)
    sent = deliver(db, chat)
    templates = {b["template"]["name"]: b for b in sent}
    assert set(templates) == {"caregiver_assigned", "visit_code"}
    assert template_params(templates["visit_code"]) == [booking.booking_code, code]
    assert template_params(templates["caregiver_assigned"])[:2] == [booking.booking_code, "Ngozi"]


def test_the_visit_code_is_never_logged_or_stored_in_the_outbox(db, chat, caplog):
    booking, _ = accepted_booking(db, chat)
    code = decrypt(booking.visit_code_enc)
    with caplog.at_level(logging.DEBUG):
        deliver(db, chat)
    assert f" {code}" not in caplog.text and f"is {code}" not in caplog.text
    row_text = json.dumps([(e.event, e.dedupe_key, e.last_error) for e in db.query(BookingEvent)])
    assert code not in row_text.replace(str(booking.id), "").replace(str(booking.caregiver_id), "")
    from app.db.models.whatsapp import WhatsAppMessage
    assert not [m for m in db.query(WhatsAppMessage) if code in json.dumps({c.name: str(getattr(m, c.name))
                                                                           for c in m.__table__.columns})]


def test_journey_messages_follow_the_visit_and_each_is_sent_once(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    deliver(db, chat)
    act(db, booking, caregiver, "on_my_way")
    en_route = deliver(db, chat)
    assert [body_text(b) for b in en_route] == [f"Booking {booking.booking_code}: Ngozi is on the way to you now."]
    act(db, booking, caregiver, "start", visit_code=decrypt(booking.visit_code_enc))
    assert [body_text(b) for b in deliver(db, chat)] == [f"The visit for booking {booking.booking_code} has started."]
    act(db, booking, caregiver, "complete")
    done = deliver(db, chat)
    assert body_text(done[0]) == (f"The visit for booking {booking.booking_code} is complete. "
                                  "Thank you for choosing Oakly.")
    assert done[0]["interactive"]["action"]["buttons"][0]["reply"]["id"] == "menu:book"
    assert deliver(db, chat) == []  # nothing is sent twice


def test_a_stale_event_is_dropped_not_announced(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    deliver(db, chat)
    act(db, booking, caregiver, "on_my_way")
    act(db, booking, caregiver, "start", visit_code=decrypt(booking.visit_code_enc))  # arrived before the worker ran
    sent = [body_text(b) for b in deliver(db, chat)]
    assert not any("on the way" in t for t in sent)
    assert any("has started" in t for t in sent)


def test_a_replaced_caregiver_is_announced_and_the_old_one_is_not(db, chat):
    booking, first = accepted_booking(db, chat)
    # the first caregiver is replaced before the worker ran: the old event no longer matches the booking
    second = make_caregiver(db, full_name="Amaka Eze", years_of_experience=1)
    db.query(Booking).filter_by(id=booking.id).update({"caregiver_id": second.id})
    db.commit()
    db.expire_all()
    NotificationService(db).caregiver_accepted(db.get(Booking, booking.id))
    db.commit()
    sent = [body_text(b) for b in deliver(db, chat)]
    assert any("Amaka will visit" in t and "Experience: 1 year." in t for t in sent)
    assert not any("Ngozi" in t for t in sent)


def test_emitting_the_same_event_twice_queues_it_once(db, chat):
    booking, _ = accepted_booking(db, chat)
    NotificationService(db).caregiver_accepted(booking)
    db.commit()
    assert len(events_of(db, booking)) == 2


def test_stop_skips_the_non_essential_messages_but_the_visit_code_still_goes(db, chat):
    chat.say("STOP")
    booking, _ = accepted_booking(db, chat)
    sent = [body_text(b) for b in deliver(db, chat)]
    assert len(sent) == 1 and "Your visit code" in sent[0]  # the assignment note is skipped, the code is essential
    db.expire_all()
    assert db.query(BookingEvent).filter_by(event="caregiver_assigned").one().last_error == "opted_out"


def test_two_workers_delivering_at_once_send_one_copy(db, chat):
    import threading
    from app.db.session import SessionLocal
    accepted_booking(db, chat)
    before = len(chat.fake.bodies)

    def worker():
        s = SessionLocal()
        try:
            notifications.deliver_pending(s)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(chat.fake.bodies) - before == 2  # assigned + code, once each


# --- the caregiver cancels -----------------------------------------------------------------------------------
def cancel_mid_visit(db, chat, booking, caregiver):
    """Since the re-dispatch work, a cancel *before* the visit reopens the booking (test_dropout_redispatch); the
    refund-and-tell wording below is what remains for a visit that had started."""
    act(db, booking, caregiver, "start", visit_code=decrypt(booking.visit_code_enc))
    deliver(db, chat)  # "the visit has started" goes out first
    act(db, booking, caregiver, "cancel")


def test_caregiver_cancel_mid_visit_leaves_the_amount_to_a_person(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    act(db, booking, caregiver, "start", visit_code=decrypt(booking.visit_code_enc))
    deliver(db, chat)
    act(db, booking, caregiver, "cancel")
    db.expire_all()
    booking = db.get(Booking, booking.id)
    assert booking.refund_required and booking.refund_amount_kobo is None
    text = body_text(deliver(db, chat)[0])
    assert "Our team will contact you about any refund" in text and "₦" not in text


def test_caregiver_cancel_leaves_an_app_booking_exactly_as_before(db, chat):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, caregiver, status=BookingStatus.ACCEPTED, visit_code="1234")
    act(db, booking, caregiver, "cancel")
    db.expire_all()
    booking = db.get(Booking, booking.id)
    assert booking.status == BookingStatus.CANCELLED and not booking.refund_required
    assert events_of(db, booking) == []


def test_cancel_notice_outside_the_window_is_the_booking_update_template(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    deliver(db, chat)
    cancel_mid_visit(db, chat, booking, caregiver)
    leave_window(db, chat)
    sent = deliver(db, chat)
    assert sent[0]["template"]["name"] == "booking_update"
    assert template_params(sent[0])[0] == booking.booking_code


def test_the_cancel_notice_still_reaches_someone_who_sent_stop(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    deliver(db, chat)
    chat.say("STOP")
    cancel_mid_visit(db, chat, booking, caregiver)
    assert len(deliver(db, chat)) == 1


# --- admin cancellation and refunds (the hooks the admin console calls) -----------------------------------------
def test_admin_cancel_and_refund_events_are_worded_from_the_booking(db, chat):
    booking, caregiver = accepted_booking(db, chat)
    deliver(db, chat)
    booking.status = BookingStatus.CANCELLED
    booking.refund_required, booking.refund_amount_kobo = True, 3_520_000
    NotificationService(db).cancelled_by_admin(booking)
    NotificationService(db).refund_processed(booking)
    db.commit()
    texts = sorted(body_text(b) for b in deliver(db, chat))
    assert texts == sorted([
        f"Update on booking {booking.booking_code}: Our team has cancelled this visit. Reply here if you need help.",
        f"Your refund of ₦35,200 for booking {booking.booking_code} is being processed. "
        "Reply here if you have questions.",
        f"Your refund of ₦35,200 for booking {booking.booking_code} is complete. Reply here if you have questions."])


def test_refund_flagged_says_it_is_being_processed(db, chat):
    booking = wa_booking(db, chat)
    booking.refund_required, booking.refund_amount_kobo = True, 1_000_000
    NotificationService(db).refund_flagged(booking)
    db.commit()
    assert body_text(deliver(db, chat)[0]) == (
        f"Your refund of ₦10,000 for booking {booking.booking_code} is being processed. "
        "Reply here if you have questions.")


# --- the caregiver's push ---------------------------------------------------------------------------------------------
def test_a_cancellation_pushes_the_assigned_caregiver_without_personal_details(db, chat, expo):
    from app.tests.whatsapp_notify_helpers import contact_patient  # noqa: F401
    booking, caregiver = accepted_booking(db, chat)
    db.add(PushToken(user_id=caregiver.user_id, token="ExponentPushToken[cg-w3]", platform="ios"))
    db.commit()
    booking.status = BookingStatus.CANCELLED
    NotificationService(db).cancelled_by_booker(booking)
    db.commit()
    deliver(db, chat)
    assert len(expo.messages) == 1
    message = expo.messages[0]
    assert message["to"] == "ExponentPushToken[cg-w3]"
    assert message["body"] == f"Booking {booking.booking_code} has been cancelled."
    assert message["data"] == {"type": "booking_cancelled", "booking_id": str(booking.id)}
    blob = json.dumps(message)
    assert "SENTINEL" not in blob and booking.patient_address not in blob


def test_a_caregiver_push_is_sent_for_an_app_booking_too(db, chat, expo):
    patient, caregiver = make_patient(db), make_caregiver(db)
    db.add(PushToken(user_id=caregiver.user_id, token="ExponentPushToken[cg-app]", platform="ios"))
    booking = make_booking(db, patient, caregiver, status=BookingStatus.CANCELLED)
    NotificationService(db).cancelled_by_booker(booking)
    db.commit()
    notifications.deliver_pending(db)
    assert [m["to"] for m in expo.messages] == ["ExponentPushToken[cg-app]"]


def test_no_caregiver_no_push_and_no_event(db, chat, expo):
    booking = wa_booking(db, chat, status=BookingStatus.CANCELLED)
    NotificationService(db).cancelled_by_booker(booking)
    db.commit()
    assert events_of(db, booking) == [] and expo.messages == []


def test_a_caregiver_with_no_device_settles_the_event_without_retrying(db, chat, expo):
    booking, caregiver = accepted_booking(db, chat)
    booking.status = BookingStatus.CANCELLED
    NotificationService(db).cancelled_by_booker(booking)
    db.commit()
    deliver(db, chat)
    event = db.query(BookingEvent).filter_by(event="caregiver_booking_cancelled").one()
    assert event.delivered_at is not None and event.last_error == "no_device" and expo.messages == []
