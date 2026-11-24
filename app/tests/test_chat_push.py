"""A saved chat message pushes the other person in the booking chat (best effort); the sender never gets one."""

import logging

import httpx

from app.db.models.booking import BookingStatus, ChatMessage
from app.db.models.push_token import PushToken
from app.services import chat_push, push
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient


def token_for(db, user, name):
    token = f"ExponentPushToken[{name}]"
    db.add(PushToken(user_id=user.id, token=token, platform="android"))
    db.commit()
    return token


def setup(db, patient_name="Ada Okafor", caregiver_name="Chidi Eze Nwosu"):
    patient = make_patient(db, full_name=patient_name)
    cg = make_caregiver(db, full_name=caregiver_name)
    booking = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED)
    return patient, cg, booking


def send(client, user, booking, text):
    return client.post(f"/api/v1/booking/chat/{booking.id}/send", headers=auth_headers(user), json={"message": text})


# --- who gets what ---------------------------------------------------------------------


def test_caregiver_message_pushes_the_patient_with_the_caregivers_first_name(db, client, expo):
    patient, cg, booking = setup(db)
    patient_token = token_for(db, patient.user, "patient")
    cg_token = token_for(db, cg.user, "caregiver")

    r = send(client, cg.user, booking, "I'm on my way")

    assert r.status_code == 200
    assert [m["to"] for m in expo.messages] == [patient_token]  # never the sender's own device
    msg = expo.messages[0]
    assert msg["title"] == "Chidi"
    assert msg["body"] == "I'm on my way"
    assert msg["data"] == {"type": "chat_message", "booking_id": str(booking.id)}
    assert cg_token not in expo.raw_bodies


def test_patient_message_pushes_the_caregiver_with_first_name_and_last_initial(db, client, expo):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")
    cg_token = token_for(db, cg.user, "caregiver")

    assert send(client, patient.user, booking, "Gate code is at the desk").status_code == 200

    assert [m["to"] for m in expo.messages] == [cg_token]
    msg = expo.messages[0]
    assert msg["title"] == "Ada O."
    assert "Okafor" not in expo.raw_bodies  # the surname never goes out
    assert msg["data"] == {"type": "chat_message", "booking_id": str(booking.id)}


def test_a_long_message_is_cut_to_about_100_characters_on_one_line(db, client, expo):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")
    send(client, cg.user, booking, "line one\n\n" + "word " * 60)
    body = expo.messages[0]["body"]
    assert len(body) <= chat_push.BODY_CHARS and body.endswith("…") and "\n" not in body
    assert body.startswith("line one word word")


def test_a_message_of_exactly_100_characters_is_not_cut(db, client, expo):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")
    send(client, cg.user, booking, "x" * 100)
    assert expo.messages[0]["body"] == "x" * 100


# --- nobody to push --------------------------------------------------------------------


def test_no_token_no_push_and_the_send_still_succeeds(db, client, expo):
    patient, cg, booking = setup(db)
    r = send(client, cg.user, booking, "hello")
    assert r.status_code == 200 and expo.requests == []


def test_a_booking_without_a_caregiver_has_nobody_to_push(db, expo):
    patient = make_patient(db)
    booking = make_booking(db, patient, None)
    assert chat_push.push_new_message(booking.id, patient.user_id, "patient", "hi") == 0
    assert expo.requests == []


def test_a_deactivated_recipient_gets_nothing(db, client, expo):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")
    patient.user.is_active = False
    db.commit()
    assert send(client, cg.user, booking, "hello").status_code == 200
    assert expo.requests == []


def test_someone_outside_the_booking_cannot_send_and_nothing_is_pushed(db, client, expo):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")
    stranger = make_caregiver(db)
    r = send(client, stranger.user, booking, "hello")
    assert r.status_code == 403
    assert expo.requests == [] and db.query(ChatMessage).filter_by(booking_id=booking.id).count() == 0


# --- best effort -----------------------------------------------------------------------


def test_a_failing_expo_never_fails_the_send(db, client, expo, caplog):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")
    expo.raise_error = httpx.ConnectError("expo is down")
    with caplog.at_level(logging.WARNING):
        r = send(client, cg.user, booking, "hello")
    assert r.status_code == 200
    assert db.query(ChatMessage).filter_by(booking_id=booking.id).count() == 1


def test_an_unexpected_error_in_the_push_path_is_swallowed(db, client, expo, monkeypatch):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")

    def boom(self, *a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(push.PushService, "send_to_users", boom)
    r = send(client, cg.user, booking, "hello")
    assert r.status_code == 200
    assert db.query(ChatMessage).filter_by(booking_id=booking.id).count() == 1


def test_a_dead_token_is_pruned_like_every_other_push(db, client, expo):
    patient, cg, booking = setup(db)
    token = token_for(db, patient.user, "dead")
    expo.not_registered.add(token)
    send(client, cg.user, booking, "hello")
    assert db.query(PushToken).filter_by(token=token).count() == 0


# --- name helpers ----------------------------------------------------------------------


def test_name_helpers():
    assert chat_push.patient_label("Ada Okafor") == "Ada O."
    assert chat_push.patient_label("ada  mary  okafor") == "ada O."
    assert chat_push.patient_label("Ada") == "Ada" and chat_push.patient_label("") == "" and chat_push.patient_label(None) == ""
    assert chat_push.first_name("Chidi Eze") == "Chidi" and chat_push.first_name(None) == ""


def test_an_unnamed_sender_falls_back_to_a_generic_title(db, client, expo):
    patient, cg, booking = setup(db)
    token_for(db, patient.user, "patient")
    cg.full_name = " "
    db.commit()
    send(client, cg.user, booking, "hello")
    assert expo.messages[0]["title"] == chat_push.FALLBACK_TITLE
