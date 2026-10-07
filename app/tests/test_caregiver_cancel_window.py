"""A caregiver may drop a WhatsApp booking from the app only while it is ACCEPTED and more than
dropout_redispatch_min_lead_minutes before the visit. Later, or once under way, they contact support."""

from datetime import datetime, timedelta

import pytest

from app.db.models.booking import Booking, BookingStatus
from app.db.models.dropout import CaregiverDropout
from app.core.security import decrypt
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient
from app.tests.policy_helpers import set_policy
from app.tests.whatsapp_notify_helpers import (  # noqa: F401
    Chat, accepted_booking, act, fake_meta, onboarded,
)

ACTION = "/api/v1/booking/caregiver/action"
BOOKING = "/api/v1/booking/booking"
REFUSED = "It's too close to the visit to cancel in the app. Please contact support."


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def cancel(client, booking, cg):
    return client.post(f"{ACTION}/{booking.id}", json={"action": "cancel"}, headers=auth_headers(cg.user))


def wa_accepted(db, chat, minutes_ahead):
    """Accepted WhatsApp booking, then moved so the visit is `minutes_ahead` minutes away (30 s of margin so the
    whole-minute floor the check uses is unambiguous)."""
    booking, cg = accepted_booking(db, chat, hours_ahead=48)
    booking.scheduled_date = datetime.utcnow() + timedelta(minutes=minutes_ahead, seconds=30)
    booking.scheduled_end_date = booking.scheduled_date + timedelta(hours=4)
    db.commit()
    return booking, cg


def state(db, booking):
    db.expire_all()
    b = db.get(Booking, booking.id)
    return b.status, b.caregiver_id, b.dropout_state


def assert_refused_and_untouched(db, client, booking, cg):
    before = state(db, booking)
    r = cancel(client, booking, cg)
    assert r.status_code == 409, r.text
    assert r.json() == {"detail": REFUSED}
    assert state(db, booking) == before
    assert db.query(CaregiverDropout).filter_by(booking_id=booking.id).count() == 0


def test_refused_119_minutes_before_the_visit(db, client, chat):
    booking, cg = wa_accepted(db, chat, 119)
    assert_refused_and_untouched(db, client, booking, cg)
    assert state(db, booking)[0] == BookingStatus.ACCEPTED


def test_refused_at_exactly_the_policy_lead(db, client, chat):
    booking, cg = wa_accepted(db, chat, 120)
    booking.scheduled_date = datetime.utcnow() + timedelta(minutes=120, seconds=-5)
    db.commit()
    assert_refused_and_untouched(db, client, booking, cg)


def test_allowed_121_minutes_before_the_visit_and_reopens_for_a_replacement(db, client, chat):
    booking, cg = wa_accepted(db, chat, 121)
    r = cancel(client, booking, cg)
    assert r.status_code == 200, r.text
    status, caregiver_id, dropout_state = state(db, booking)
    assert status == BookingStatus.PENDING and caregiver_id is None and dropout_state == "redispatching"


def test_refused_en_route_even_with_plenty_of_time(db, client, chat):
    booking, cg = wa_accepted(db, chat, 119)  # on-my-way is only allowed inside the window
    act(db, booking, cg, "on_my_way")
    assert state(db, booking)[0] == BookingStatus.EN_ROUTE
    assert_refused_and_untouched(db, client, booking, cg)


def test_refused_en_route_when_far_from_the_visit_too(db, client, chat):
    booking, cg = wa_accepted(db, chat, 119)
    act(db, booking, cg, "on_my_way")
    booking.scheduled_date = datetime.utcnow() + timedelta(hours=5)  # status, not the clock, decides
    db.commit()
    assert_refused_and_untouched(db, client, booking, cg)


def test_refused_in_progress(db, client, chat):
    booking, cg = wa_accepted(db, chat, 60)
    act(db, booking, cg, "start", visit_code=decrypt(booking.visit_code_enc))
    assert state(db, booking)[0] == BookingStatus.IN_PROGRESS
    assert_refused_and_untouched(db, client, booking, cg)


def test_the_policy_lead_is_respected(db, client, chat):
    set_policy(db, dropout_redispatch_min_lead_minutes=60, dropout_decision_lead_minutes=30)
    inside, cg_in = wa_accepted(db, chat, 59)
    assert_refused_and_untouched(db, client, inside, cg_in)
    outside, cg_out = wa_accepted(db, chat, 61)
    assert cancel(client, outside, cg_out).status_code == 200
    assert state(db, outside)[2] == "redispatching"


def test_an_app_booking_is_unaffected_close_to_the_visit_and_in_progress(db, client):
    patient = make_patient(db)
    for status in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS):
        cg = make_caregiver(db)
        soon = datetime.utcnow() + timedelta(minutes=30)
        booking = make_booking(db, patient, cg, status=status, visit_code="1234",
                               scheduled_date=soon, scheduled_end_date=soon + timedelta(hours=4))
        r = cancel(client, booking, cg)
        assert r.status_code == 200, r.text
        assert state(db, booking)[0] == BookingStatus.CANCELLED


def test_a_booking_that_is_no_longer_cancellable_keeps_its_own_message(db, client, chat):
    booking, cg = wa_accepted(db, chat, 30)
    booking.status = BookingStatus.COMPLETED
    db.commit()
    r = cancel(client, booking, cg)
    assert r.status_code == 409 and r.json() == {"detail": "This booking can no longer be cancelled"}


# --- the caregiver app needs the channel to know which rule applies -------------------------------------------------
def test_channel_is_returned_by_the_booking_and_the_list_endpoints(db, client, chat):
    wa, cg = wa_accepted(db, chat, 300)
    app_booking = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, visit_code="1234")
    one = client.get(f"{BOOKING}/{wa.id}", headers=auth_headers(cg.user))
    assert one.status_code == 200 and one.json()["channel"] == "whatsapp"
    listing = client.get("/api/v1/booking/caregiver/bookings", headers=auth_headers(cg.user))
    assert listing.status_code == 200
    by_id = {b["id"]: b for b in listing.json()}
    assert by_id[str(wa.id)]["channel"] == "whatsapp" and by_id[str(app_booking.id)]["channel"] == "app"


def test_channel_is_returned_in_the_action_response(db, client, chat):
    booking, cg = accepted_booking(db, chat, hours_ahead=10)
    r = client.post(f"{ACTION}/{booking.id}", json={"action": "on_my_way"}, headers=auth_headers(cg.user))
    assert r.status_code == 409  # not inside the window yet; the booking body is on success only
    set_policy(db, onmyway_compensation_window_minutes=720)
    r = client.post(f"{ACTION}/{booking.id}", json={"action": "on_my_way"}, headers=auth_headers(cg.user))
    assert r.status_code == 200 and r.json()["booking"]["channel"] == "whatsapp"


def test_channel_is_not_hidden_from_an_offered_caregiver(db, client, chat):
    from app.tests.whatsapp_notify_helpers import offer, wa_booking
    booking = wa_booking(db, chat, hours_ahead=10)
    stranger = make_caregiver(db)
    offer(db, booking, stranger)
    r = client.get("/api/v1/booking/caregiver/bookings?status=new_requests", headers=auth_headers(stranger.user))
    assert r.status_code == 200, r.text
    (item,) = r.json()
    assert item["channel"] == "whatsapp" and item["patient_details"] is None
