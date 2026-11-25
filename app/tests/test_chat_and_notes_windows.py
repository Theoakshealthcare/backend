"""The chat closes 24 h after the visit (read-only, ``chat_closed``) and typed visit notes follow the voice-note windows."""

from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models.booking import BookingStatus, ChatMessage
from app.db.models.push_token import PushToken
from app.services import visit_windows
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

SEND = "/api/v1/booking/chat/{}/send"
HISTORY = "/api/v1/booking/chat/{}/messages"
HOURS = timedelta(hours=1)


def ended(hours_ago):
    return datetime.utcnow() - timedelta(hours=hours_ago)


def completed(db, patient, cg, hours_ago):
    return make_booking(db, patient, cg, status=BookingStatus.COMPLETED, actual_end_time=ended(hours_ago),
                        scheduled_date=ended(hours_ago + 4), scheduled_end_date=ended(hours_ago))


def say(client, user, booking, text="hello"):
    return client.post(SEND.format(booking.id), headers=auth_headers(user), json={"message": text})


@pytest.fixture
def pair(db):
    return make_patient(db), make_caregiver(db)


# --- the window itself ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [BookingStatus.PENDING, BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS])
def test_chat_is_open_while_the_booking_is_not_over(db, pair, status):
    booking = make_booking(db, *pair, status=status)
    window = visit_windows.chat_window(booking, datetime.utcnow())
    assert (window.open, window.closes_at) == (True, None)


def test_chat_is_open_for_24h_after_completion_and_closes_at_the_end_plus_24h(db, pair):
    end = datetime(2026, 11, 25, 10, 0)
    booking = make_booking(db, *pair, status=BookingStatus.COMPLETED, actual_end_time=end)
    inside = visit_windows.chat_window(booking, end + timedelta(hours=23, minutes=59))
    assert (inside.open, inside.closes_at) == (True, end + timedelta(hours=settings.CHAT_OPEN_HOURS_AFTER_VISIT))
    assert visit_windows.chat_window(booking, end + timedelta(hours=24, minutes=1)).open is False


@pytest.mark.parametrize("status", [BookingStatus.CANCELLED, BookingStatus.DECLINED])
def test_cancelled_or_declined_is_read_only_at_once(db, pair, status):
    window = visit_windows.chat_window(make_booking(db, *pair, status=status), datetime.utcnow())
    assert (window.open, window.closes_at) == (False, None)


# --- REST send and read ---------------------------------------------------------------------


def test_both_people_can_send_while_open_and_inside_the_24h(db, client, pair):
    patient, cg = pair
    booking = completed(db, patient, cg, hours_ago=23)
    assert say(client, cg.user, booking, "thanks for today").status_code == 200
    assert say(client, patient.user, booking, "thank you").status_code == 200


@pytest.mark.parametrize("who", ["patient", "caregiver"])
def test_sending_after_24h_is_409_chat_closed_and_saves_and_pushes_nothing(db, client, expo, pair, who):
    patient, cg = pair
    for user in (patient.user, cg.user):
        db.add(PushToken(user_id=user.id, token=f"ExponentPushToken[{user.id.hex[:10]}]", platform="ios"))
    db.commit()
    booking = completed(db, patient, cg, hours_ago=25)
    sender = patient.user if who == "patient" else cg.user
    r = say(client, sender, booking)
    assert r.status_code == 409
    assert r.json() == {"detail": {"code": "chat_closed", "message": visit_windows.CHAT_CLOSED_MESSAGE}}
    assert db.query(ChatMessage).filter_by(booking_id=booking.id).count() == 0
    assert expo.messages == []


@pytest.mark.parametrize("who", ["patient", "caregiver"])
def test_a_cancelled_booking_is_read_only_at_once(db, client, pair, who):
    patient, cg = pair
    booking = make_booking(db, patient, cg, status=BookingStatus.CANCELLED)
    r = say(client, patient.user if who == "patient" else cg.user, booking)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "chat_closed"


def test_history_stays_readable_by_both_after_the_chat_closed(db, client, pair):
    patient, cg = pair
    booking = completed(db, patient, cg, hours_ago=23)
    assert say(client, cg.user, booking, "see you next week").status_code == 200
    booking.actual_end_time = ended(48)  # time passes: the chat is now closed
    db.commit()
    for user in (patient.user, cg.user):
        r = client.get(HISTORY.format(booking.id), headers=auth_headers(user))
        assert r.status_code == 200 and [m["message"] for m in r.json()] == ["see you next week"]


def test_access_rules_still_come_first(db, client, pair):
    outsider = make_caregiver(db)
    booking = completed(db, *pair, hours_ago=30)
    assert say(client, outsider.user, booking).status_code == 403  # not chat_closed: not their booking


# --- the fields the apps read ---------------------------------------------------------------------


def test_chat_fields_on_the_caregiver_chat_list(db, client, pair):
    patient, cg = pair
    open_one = completed(db, patient, cg, hours_ago=1)
    closed_one = completed(db, patient, cg, hours_ago=30)
    live = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED)
    gone = make_booking(db, patient, cg, status=BookingStatus.CANCELLED)
    items = {i["booking_id"]: i for i in client.get("/api/v1/caregiver/chats", headers=auth_headers(cg.user)).json()["items"]}
    assert items[str(live.id)]["chat_open"] is True and items[str(live.id)]["chat_closes_at"] is None
    assert items[str(open_one.id)]["chat_open"] is True and items[str(open_one.id)]["chat_closes_at"] is not None
    assert items[str(closed_one.id)]["chat_open"] is False and items[str(closed_one.id)]["chat_closes_at"] is not None
    assert items[str(gone.id)]["chat_open"] is False and items[str(gone.id)]["chat_closes_at"] is None


def test_chat_fields_on_the_patient_booking_payloads_and_notes_fields_are_caregiver_only(db, client, pair):
    patient, cg = pair
    booking = completed(db, patient, cg, hours_ago=1)
    detail = client.get(f"/api/v1/booking/booking/{booking.id}", headers=auth_headers(patient.user)).json()
    assert detail["chat_open"] is True and detail["chat_closes_at"] is not None
    assert (detail["notes_open"], detail["notes_closes_at"], detail["notes_readable_until"]) == (None, None, None)
    listing = client.get("/api/v1/booking/patient/bookings", headers=auth_headers(patient.user)).json()
    assert listing[0]["chat_open"] is True and listing[0]["notes_open"] is None
    cancelled = make_booking(db, patient, cg, status=BookingStatus.CANCELLED)
    detail = client.get(f"/api/v1/booking/booking/{cancelled.id}", headers=auth_headers(patient.user)).json()
    assert detail["chat_open"] is False and detail["chat_closes_at"] is None


# --- typed notes ---------------------------------------------------------------------

CREATE = "/api/v1/booking/notes/{}/create"
NOTES = "/api/v1/booking/notes/{}"


def add_note(client, user, booking):
    return client.post(CREATE.format(booking.id), headers=auth_headers(user), json={"title": "Visit", "content": "All well."})


def test_a_typed_note_can_be_added_in_progress_and_within_the_grace_after_completion(db, client, pair):
    patient, cg = pair
    assert add_note(client, cg.user, make_booking(db, patient, cg, status=BookingStatus.IN_PROGRESS)).status_code == 200
    assert add_note(client, cg.user, completed(db, patient, cg, hours_ago=1.9)).status_code == 200


def test_a_typed_note_after_the_grace_or_before_the_visit_is_409_visit_not_active(db, client, pair):
    patient, cg = pair
    for booking in (completed(db, patient, cg, hours_ago=3), make_booking(db, patient, cg, status=BookingStatus.ACCEPTED)):
        r = add_note(client, cg.user, booking)
        assert r.status_code == 409
        assert r.json() == {"detail": {"code": "visit_not_active", "message": visit_windows.NOTES_CLOSED_MESSAGE}}


def test_a_suspended_caregiver_keeps_only_a_visit_in_progress(db, client):
    patient = make_patient(db)
    cg = make_caregiver(db, approved=False)
    assert add_note(client, cg.user, completed(db, patient, cg, hours_ago=1)).status_code == 404
    assert add_note(client, cg.user, make_booking(db, patient, cg, status=BookingStatus.IN_PROGRESS)).status_code == 200


def test_notes_are_readable_by_the_caregiver_for_24h_and_by_the_patient_always(db, client, pair):
    patient, cg = pair
    booking = completed(db, patient, cg, hours_ago=1)
    assert add_note(client, cg.user, booking).status_code == 200
    assert len(client.get(NOTES.format(booking.id), headers=auth_headers(cg.user)).json()) == 1
    booking.actual_end_time = ended(25)
    db.commit()
    r = client.get(NOTES.format(booking.id), headers=auth_headers(cg.user))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    assert len(client.get(NOTES.format(booking.id), headers=auth_headers(patient.user)).json()) == 1  # the patient is not windowed


def test_notes_fields_on_the_caregiver_booking_payloads(db, client, pair):
    patient, cg = pair
    live = make_booking(db, patient, cg, status=BookingStatus.IN_PROGRESS)
    recent = completed(db, patient, cg, hours_ago=1)
    old = completed(db, patient, cg, hours_ago=3)
    rows = {b["id"]: b for b in client.get("/api/v1/booking/caregiver/bookings", headers=auth_headers(cg.user)).json()}
    assert (rows[str(live.id)]["notes_open"], rows[str(live.id)]["notes_closes_at"], rows[str(live.id)]["notes_readable_until"]) == (True, None, None)
    assert rows[str(recent.id)]["notes_open"] is True and rows[str(recent.id)]["notes_closes_at"] is not None
    assert rows[str(recent.id)]["notes_readable_until"] is not None
    assert rows[str(old.id)]["notes_open"] is False and rows[str(old.id)]["notes_readable_until"] is not None
    detail = client.get(f"/api/v1/booking/booking/{recent.id}", headers=auth_headers(cg.user)).json()
    assert detail["notes_open"] is True and detail["chat_open"] is True
