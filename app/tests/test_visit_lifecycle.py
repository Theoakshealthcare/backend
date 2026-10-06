"""C3 item 4: on_my_way (accepted -> en_route) and the 4-digit visit code required to start."""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus, CaregiverLocation
from app.main import app as fastapi_app
from app.tests.factories import (
    auth_headers,
    make_admin,
    make_booking,
    make_caregiver,
    make_patient,
)
from app.tests.test_access_booking import notify

ACTION = "/api/v1/booking/caregiver/action"
BOOKING = "/api/v1/booking/booking"
CODE = "4821"
WRONG = "0000"


def act(client, booking, cg, action, **extra):
    return client.post(f"{ACTION}/{booking.id}", json={"action": action, **extra},
                       headers=auth_headers(cg.user))


def leaks_code(response, code=CODE) -> bool:
    """True if any JSON value equals the code. (A substring check would flake: 4 digits turn up in
    timestamps, ids and phone numbers.)"""
    def walk(o):
        if isinstance(o, dict):
            return any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        return o == code
    return walk(response.json())


def get_booking(client, booking, user):
    return client.get(f"{BOOKING}/{booking.id}", headers=auth_headers(user))


def reload(db, booking):
    db.expire_all()
    return db.get(Booking, booking.id)


@pytest.fixture
def visit(db):
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, visit_code=CODE)
    return patient, cg, booking


# --- the code is generated at accept time -----------------------------------------


def accepted_via_api(db, client):
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient)
    notify(db, booking, cg)
    r = act(client, booking, cg, "accept")
    assert r.status_code == 200, r.text
    return patient, cg, booking, r


def test_accept_generates_a_four_digit_code_stored_encrypted(db, client):
    patient, cg, booking, r = accepted_via_api(db, client)
    row = reload(db, booking)
    assert row.visit_code_enc is not None
    code = decrypt(row.visit_code_enc)
    assert re.fullmatch(r"\d{4}", code)
    assert code not in row.visit_code_enc  # not stored in the clear
    assert row.visit_code_failed_attempts == 0 and row.visit_code_locked_until is None


def test_codes_are_not_constant(db, client):
    codes = set()
    for _ in range(12):
        *_, booking, _r = accepted_via_api(db, client)
        codes.add(decrypt(reload(db, booking).visit_code_enc))
    assert len(codes) > 1


def test_the_accept_response_never_leaks_the_code_or_its_ciphertext(db, client):
    patient, cg, booking, r = accepted_via_api(db, client)
    row = reload(db, booking)
    text = r.text
    assert row.visit_code_enc not in text
    assert "visit_code_enc" not in text and "visit_code_failed_attempts" not in text
    body = r.json()["booking"]
    assert body.get("visit_code") is None


# --- who can read the code ----------------------------------------------------------


@pytest.mark.parametrize("status", [BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE])
def test_only_the_bookings_patient_sees_the_code(db, client, status):
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, cg, status=status, visit_code=CODE)
    admin, stranger = make_admin(db), make_patient(db)

    r = get_booking(client, booking, patient.user)
    assert r.status_code == 200 and r.json()["visit_code"] == CODE

    r = get_booking(client, booking, cg.user)
    assert r.status_code == 200 and r.json()["visit_code"] is None and not leaks_code(r)
    r = get_booking(client, booking, admin)
    assert r.status_code == 200 and r.json()["visit_code"] is None and not leaks_code(r)
    assert get_booking(client, booking, stranger.user).status_code == 403


@pytest.mark.parametrize("status", [BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED, BookingStatus.CANCELLED])
def test_the_code_is_hidden_once_the_visit_has_started_or_ended(db, client, status):
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, cg, status=status, visit_code=CODE)
    r = get_booking(client, booking, patient.user)
    assert r.json()["visit_code"] is None and not leaks_code(r)


def test_the_code_is_not_in_lists_or_the_ciphertext_anywhere(db, client, visit):
    patient, cg, booking = visit
    for path, user in (("/patient/bookings", patient.user), ("/caregiver/bookings", cg.user)):
        r = client.get(f"/api/v1/booking{path}", headers=auth_headers(user))
        assert r.status_code == 200
        assert not leaks_code(r) and "visit_code_enc" not in r.text
    r = get_booking(client, booking, patient.user)
    assert "visit_code_enc" not in r.text


# --- on_my_way ----------------------------------------------------------------------


def test_on_my_way_moves_accepted_to_en_route(db, client, visit):
    patient, cg, booking = visit
    r = act(client, booking, cg, "on_my_way")
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Booking on_my_way successfully"
    assert r.json()["booking"]["status"] == "en_route"
    assert reload(db, booking).status == BookingStatus.EN_ROUTE


def test_on_my_way_requires_the_assigned_caregiver(db, client, visit):
    patient, owner, booking = visit
    other = make_caregiver(db)
    notify(db, booking, other)
    assert act(client, booking, other, "on_my_way").status_code == 403
    assert client.post(f"{ACTION}/{booking.id}", json={"action": "on_my_way"}).status_code == 401
    assert client.post(f"{ACTION}/{booking.id}", json={"action": "on_my_way"},
                       headers=auth_headers(patient.user)).status_code == 403
    assert reload(db, booking).status == BookingStatus.ACCEPTED


@pytest.mark.parametrize(
    "source",
    [BookingStatus.PENDING, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED,
     BookingStatus.CANCELLED, BookingStatus.DECLINED],
)
def test_on_my_way_from_any_other_state_is_a_conflict(db, client, source):
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, cg, status=source, visit_code=CODE)
    r = act(client, booking, cg, "on_my_way")
    assert r.status_code == 409
    assert r.json()["detail"] == "Only an accepted booking can be marked on the way"
    assert reload(db, booking).status == source


def test_cancel_is_allowed_from_en_route_and_complete_is_not(db, client):
    patient, cg = make_patient(db), make_caregiver(db)
    b1 = make_booking(db, patient, cg, status=BookingStatus.EN_ROUTE, visit_code=CODE)
    b2 = make_booking(db, patient, cg, status=BookingStatus.EN_ROUTE, visit_code=CODE)
    assert act(client, b2, cg, "complete").status_code == 409
    assert act(client, b1, cg, "cancel").status_code == 200
    assert reload(db, b1).status == BookingStatus.CANCELLED


def test_live_tracking_works_while_en_route(db, client):
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, cg, status=BookingStatus.EN_ROUTE, visit_code=CODE)
    db.add(CaregiverLocation(caregiver_id=cg.id, latitude=6.45, longitude=3.47, is_active=True))
    db.commit()
    r = client.get(f"{BOOKING}/{booking.id}/live-tracking", headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text


# --- start requires the code --------------------------------------------------------


@pytest.mark.parametrize("source", [BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE])
def test_start_with_the_right_code(db, client, source):
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, cg, status=source, visit_code=CODE)
    r = act(client, booking, cg, "start", visit_code=CODE)
    assert r.status_code == 200, r.text
    row = reload(db, booking)
    assert row.status == BookingStatus.IN_PROGRESS and row.actual_start_time is not None
    assert row.visit_code_failed_attempts == 0


def test_start_without_a_code_is_refused(db, client, visit):
    patient, cg, booking = visit
    r = act(client, booking, cg, "start")
    assert r.status_code == 400
    assert r.json()["detail"] == "A visit code is required to start the visit"
    row = reload(db, booking)
    assert row.status == BookingStatus.ACCEPTED and row.visit_code_failed_attempts == 0


@pytest.mark.parametrize("bad", ["123", "12345", "abcd", "12 4", "", "４８２１"])
def test_a_malformed_code_is_a_validation_error_and_not_an_attempt(db, client, visit, bad):
    patient, cg, booking = visit
    r = act(client, booking, cg, "start", visit_code=bad)
    assert r.status_code == 422
    assert reload(db, booking).visit_code_failed_attempts == 0


def test_wrong_code_is_refused_and_counted(db, client, visit):
    patient, cg, booking = visit
    r = act(client, booking, cg, "start", visit_code=WRONG)
    assert r.status_code == 400
    assert r.json()["detail"] == "Incorrect visit code. 2 attempts left before the code is locked"
    r = act(client, booking, cg, "start", visit_code=WRONG)
    assert r.json()["detail"] == "Incorrect visit code. 1 attempt left before the code is locked"
    row = reload(db, booking)
    assert row.status == BookingStatus.ACCEPTED and row.visit_code_failed_attempts == 2


def test_a_right_code_after_wrong_ones_resets_the_counter(db, client, visit):
    patient, cg, booking = visit
    act(client, booking, cg, "start", visit_code=WRONG)
    act(client, booking, cg, "start", visit_code=WRONG)
    assert act(client, booking, cg, "start", visit_code=CODE).status_code == 200
    assert reload(db, booking).visit_code_failed_attempts == 0


def lock(db, client, booking, cg):
    codes = [act(client, booking, cg, "start", visit_code=WRONG) for _ in range(3)]
    return codes


def test_three_wrong_attempts_lock_the_code_for_fifteen_minutes(db, client, visit):
    patient, cg, booking = visit
    first, second, third = lock(db, client, booking, cg)
    assert (first.status_code, second.status_code, third.status_code) == (400, 400, 423)
    assert third.json()["detail"] == "Too many incorrect attempts. The visit code is locked for 15 minutes"
    assert 890 <= int(third.headers["retry-after"]) <= 900

    row = reload(db, booking)
    assert row.status == BookingStatus.ACCEPTED
    assert timedelta(minutes=14) < row.visit_code_locked_until - datetime.utcnow() <= timedelta(minutes=15)


def test_while_locked_even_the_right_code_is_refused_and_does_not_extend_the_lock(db, client, visit):
    patient, cg, booking = visit
    lock(db, client, booking, cg)
    locked_until = reload(db, booking).visit_code_locked_until

    for code in (CODE, WRONG):
        r = act(client, booking, cg, "start", visit_code=code)
        assert r.status_code == 423
        assert r.json()["detail"].startswith("Too many incorrect attempts. The visit code is locked for ")
    row = reload(db, booking)
    assert row.status == BookingStatus.ACCEPTED
    assert row.visit_code_locked_until == locked_until


def test_after_the_lock_expires_the_right_code_works(db, client, visit):
    patient, cg, booking = visit
    lock(db, client, booking, cg)
    row = reload(db, booking)
    row.visit_code_locked_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert act(client, booking, cg, "start", visit_code=CODE).status_code == 200


def test_after_the_lock_expires_there_are_three_fresh_attempts(db, client, visit):
    patient, cg, booking = visit
    lock(db, client, booking, cg)
    row = reload(db, booking)
    row.visit_code_locked_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    codes = [act(client, booking, cg, "start", visit_code=WRONG).status_code for _ in range(3)]
    assert codes == [400, 400, 423]


def test_the_lock_message_rounds_remaining_time_up(db, client, visit):
    patient, cg, booking = visit
    lock(db, client, booking, cg)
    row = reload(db, booking)
    row.visit_code_locked_until = datetime.utcnow() + timedelta(seconds=61)
    db.commit()
    r = act(client, booking, cg, "start", visit_code=CODE)
    assert r.json()["detail"] == "Too many incorrect attempts. The visit code is locked for 2 minutes"
    row.visit_code_locked_until = datetime.utcnow() + timedelta(seconds=20)
    db.commit()
    r = act(client, booking, cg, "start", visit_code=CODE)
    assert r.json()["detail"] == "Too many incorrect attempts. The visit code is locked for 1 minute"


def test_concurrent_wrong_guesses_cannot_get_more_than_three_tries(db, client, visit):
    """The attempt counter is updated under the booking row lock, so parallel guesses queue up."""
    patient, cg, booking = visit
    headers = auth_headers(cg.user)
    barrier = threading.Barrier(6)

    def guess(_):
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            barrier.wait()
            return c.post(f"{ACTION}/{booking.id}", json={"action": "start", "visit_code": WRONG},
                          headers=headers).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(guess, range(6)))
    assert codes == [400, 400, 423, 423, 423, 423], codes


def test_state_is_checked_before_the_code(db, client):
    patient, cg = make_patient(db), make_caregiver(db)
    for source in (BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED, BookingStatus.CANCELLED):
        booking = make_booking(db, patient, cg, status=source, visit_code=CODE)
        r = act(client, booking, cg, "start", visit_code=WRONG)
        assert r.status_code == 409
        assert reload(db, booking).visit_code_failed_attempts == 0


def test_only_the_assigned_caregiver_can_try_the_code(db, client, visit):
    patient, owner, booking = visit
    other = make_caregiver(db)
    notify(db, booking, other)
    assert act(client, booking, other, "start", visit_code=CODE).status_code == 403
    assert act(client, booking, other, "start", visit_code=WRONG).status_code == 403
    assert reload(db, booking).visit_code_failed_attempts == 0


def test_a_booking_without_a_code_cannot_be_started(db, client):
    """Accepted before visit codes existed: fail closed rather than skip the check."""
    patient, cg = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED)
    r = act(client, booking, cg, "start", visit_code=CODE)
    assert r.status_code == 409
    assert r.json()["detail"] == "This booking has no visit code. Contact support"
    assert reload(db, booking).status == BookingStatus.ACCEPTED


def test_full_happy_path(db, client):
    patient, cg, booking, _ = accepted_via_api(db, client)
    code = get_booking(client, booking, patient.user).json()["visit_code"]
    assert re.fullmatch(r"\d{4}", code)
    assert act(client, booking, cg, "on_my_way").status_code == 200
    assert get_booking(client, booking, patient.user).json()["visit_code"] == code
    assert act(client, booking, cg, "start", visit_code=code).status_code == 200
    assert get_booking(client, booking, patient.user).json()["visit_code"] is None
    assert act(client, booking, cg, "complete").status_code == 200
    assert reload(db, booking).status == BookingStatus.COMPLETED
