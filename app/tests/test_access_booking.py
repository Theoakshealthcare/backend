"""WP-C items 5 and 6: booking read access and caregiver booking actions."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.db.models.booking import (
    Booking,
    BookingNotification,
    BookingStatus,
    CaregiverLocation,
    PaymentStatus,
)
from app.db.session import SessionLocal
from app.main import app as fastapi_app
from app.tests.factories import (
    auth_headers,
    make_admin,
    make_booking,
    make_caregiver,
    make_patient,
    make_user,
    refresh_headers,
)

BOOKING = "/api/v1/booking/booking"
ACTION = "/api/v1/booking/caregiver/action"


def notify(db, booking, caregiver):
    n = BookingNotification(
        booking_id=booking.id,
        caregiver_id=caregiver.id,
        title="New Booking Request",
        message="test",
        notification_type="new_booking",
    )
    db.add(n)
    db.commit()
    return n


def act(client, booking, caregiver, action):
    return client.post(
        f"{ACTION}/{booking.id}", json={"action": action}, headers=auth_headers(caregiver.user)
    )


# --- item 5: reading a booking / live tracking -----------------------------------


@pytest.fixture
def assigned(db):
    patient = make_patient(db)
    caregiver = make_caregiver(db)
    booking = make_booking(db, patient, caregiver, status=BookingStatus.ACCEPTED)
    db.add(CaregiverLocation(caregiver_id=caregiver.id, latitude=6.45, longitude=3.47, is_active=True))
    db.commit()
    return patient, caregiver, booking


@pytest.mark.parametrize("path", ["{id}", "{id}/live-tracking"])
def test_booking_read_access_matrix(db, client, assigned, path):
    patient, caregiver, booking = assigned
    url = f"{BOOKING}/{path.format(id=booking.id)}"
    stranger_patient = make_patient(db)
    stranger_caregiver = make_caregiver(db)
    admin = make_admin(db)
    odd_role = make_user(db, "staff")

    assert client.get(url).status_code == 401
    assert client.get(url, headers=refresh_headers(patient.user)).status_code == 401

    assert client.get(url, headers=auth_headers(patient.user)).status_code == 200
    assert client.get(url, headers=auth_headers(caregiver.user)).status_code == 200
    assert client.get(url, headers=auth_headers(admin)).status_code == 200

    assert client.get(url, headers=auth_headers(stranger_patient.user)).status_code == 403
    assert client.get(url, headers=auth_headers(stranger_caregiver.user)).status_code == 403
    # a user_type other than patient/caregiver used to fall through to "allowed"
    assert client.get(url, headers=auth_headers(odd_role)).status_code == 403


def test_booking_read_unassigned_caregiver_forbidden_on_pending_booking(db, client):
    patient = make_patient(db)
    caregiver = make_caregiver(db)
    booking = make_booking(db, patient)  # nobody assigned yet
    r = client.get(f"{BOOKING}/{booking.id}", headers=auth_headers(caregiver.user))
    assert r.status_code == 403


# --- item 6: accept ------------------------------------------------------------


def test_action_requires_token_and_caregiver_role(db, client):
    patient = make_patient(db)
    booking = make_booking(db, patient)
    url = f"{ACTION}/{booking.id}"
    assert client.post(url, json={"action": "accept"}).status_code == 401
    assert client.post(url, json={"action": "accept"}, headers=auth_headers(patient.user)).status_code == 403
    caregiver = make_caregiver(db)
    r = client.post(url, json={"action": "accept"}, headers=refresh_headers(caregiver.user))
    assert r.status_code == 401


def test_accept_by_notified_caregiver_on_paid_pending_booking(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient)
    notify(db, booking, caregiver)

    r = act(client, booking, caregiver, "accept")
    assert r.status_code == 200, r.text
    db.refresh(booking)
    assert booking.status == BookingStatus.ACCEPTED
    assert booking.caregiver_id == caregiver.id


def test_accept_by_caregiver_who_was_not_notified_is_forbidden(db, client):
    patient, notified, outsider = make_patient(db), make_caregiver(db), make_caregiver(db)
    booking = make_booking(db, patient)
    notify(db, booking, notified)

    assert act(client, booking, outsider, "accept").status_code == 403
    db.refresh(booking)
    assert booking.status == BookingStatus.PENDING
    assert booking.caregiver_id is None


@pytest.mark.parametrize("payment", [PaymentStatus.PENDING, PaymentStatus.FAILED, PaymentStatus.REFUNDED])
def test_accept_unpaid_booking_is_a_conflict(db, client, payment):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, payment_status=payment)
    notify(db, booking, caregiver)

    assert act(client, booking, caregiver, "accept").status_code == 409
    db.refresh(booking)
    assert booking.status == BookingStatus.PENDING and booking.caregiver_id is None


@pytest.mark.parametrize(
    "status", [BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.IN_PROGRESS]
)
def test_accept_non_pending_booking_is_a_conflict(db, client, status):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, status=status)
    notify(db, booking, caregiver)

    assert act(client, booking, caregiver, "accept").status_code == 409
    db.refresh(booking)
    assert booking.status == status


def test_second_caregiver_cannot_take_an_accepted_booking(db, client):
    patient, first, second = make_patient(db), make_caregiver(db), make_caregiver(db)
    booking = make_booking(db, patient)
    notify(db, booking, first)
    notify(db, booking, second)

    assert act(client, booking, first, "accept").status_code == 200
    assert act(client, booking, second, "accept").status_code == 409
    db.refresh(booking)
    assert booking.caregiver_id == first.id


def test_accepting_twice_is_a_conflict(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient)
    notify(db, booking, caregiver)
    assert act(client, booking, caregiver, "accept").status_code == 200
    assert act(client, booking, caregiver, "accept").status_code == 409


def test_concurrent_accepts_have_exactly_one_winner(db):
    patient = make_patient(db)
    booking = make_booking(db, patient)
    caregivers = [make_caregiver(db) for _ in range(6)]
    for c in caregivers:
        notify(db, booking, c)
    barrier = threading.Barrier(len(caregivers))
    # resolve ORM attributes up front: the shared test session must not be touched from threads
    url = f"{ACTION}/{booking.id}"
    headers = {c.id: auth_headers(c.user) for c in caregivers}
    booking_id, caregiver_ids = booking.id, [c.id for c in caregivers]

    def attempt(caregiver_id):
        # separate TestClient per thread so requests truly overlap
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            barrier.wait()
            return c.post(url, json={"action": "accept"}, headers=headers[caregiver_id]).status_code

    with ThreadPoolExecutor(max_workers=len(caregivers)) as pool:
        codes = list(pool.map(attempt, caregiver_ids))

    assert sorted(codes) == [200] + [409] * (len(caregivers) - 1), codes
    with SessionLocal() as fresh:
        row = fresh.get(Booking, booking_id)
        assert row.status == BookingStatus.ACCEPTED
        winners = [cid for cid, code in zip(caregiver_ids, codes) if code == 200]
        assert row.caregiver_id == winners[0]


def test_accept_unknown_booking_is_404(db, client):
    import uuid

    caregiver = make_caregiver(db)
    r = client.post(
        f"{ACTION}/{uuid.uuid4()}", json={"action": "accept"}, headers=auth_headers(caregiver.user)
    )
    assert r.status_code == 404


# --- item 6: decline -----------------------------------------------------------


def test_decline_by_notified_caregiver(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient)
    n = notify(db, booking, caregiver)

    assert act(client, booking, caregiver, "decline").status_code == 200
    db.refresh(n)
    assert n.response == "declined"
    db.refresh(booking)
    assert booking.status == BookingStatus.PENDING


def test_decline_by_caregiver_who_was_not_notified_is_forbidden(db, client):
    patient, outsider = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient)
    assert act(client, booking, outsider, "decline").status_code == 403


# --- item 6: start / complete / cancel ----------------------------------------


@pytest.mark.parametrize(
    "action,source,target",
    [
        ("start", BookingStatus.ACCEPTED, BookingStatus.IN_PROGRESS),
        ("complete", BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED),
        ("cancel", BookingStatus.ACCEPTED, BookingStatus.CANCELLED),
        ("cancel", BookingStatus.IN_PROGRESS, BookingStatus.CANCELLED),
    ],
)
def test_assigned_caregiver_can_move_booking_forward(db, client, action, source, target):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, caregiver, status=source)

    r = act(client, booking, caregiver, action)
    assert r.status_code == 200, r.text
    db.refresh(booking)
    assert booking.status == target


@pytest.mark.parametrize("action", ["start", "complete", "cancel"])
def test_other_caregiver_cannot_act_on_someone_elses_booking(db, client, action):
    patient, owner, other = make_patient(db), make_caregiver(db), make_caregiver(db)
    source = BookingStatus.IN_PROGRESS if action == "complete" else BookingStatus.ACCEPTED
    booking = make_booking(db, patient, owner, status=source)
    notify(db, booking, other)

    assert act(client, booking, other, action).status_code == 403
    db.refresh(booking)
    assert booking.status == source
    assert booking.caregiver_id == owner.id


@pytest.mark.parametrize("action", ["start", "complete", "cancel"])
def test_unassigned_caregiver_cannot_act_on_pending_booking(db, client, action):
    patient, notified = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient)
    notify(db, booking, notified)

    assert act(client, booking, notified, action).status_code == 403
    db.refresh(booking)
    assert booking.status == BookingStatus.PENDING


@pytest.mark.parametrize(
    "action,source",
    [
        ("start", BookingStatus.IN_PROGRESS),
        ("start", BookingStatus.COMPLETED),
        ("start", BookingStatus.CANCELLED),
        ("complete", BookingStatus.ACCEPTED),
        ("complete", BookingStatus.COMPLETED),
        ("complete", BookingStatus.CANCELLED),
        ("cancel", BookingStatus.COMPLETED),
        ("cancel", BookingStatus.CANCELLED),
    ],
)
def test_invalid_source_state_is_a_conflict(db, client, action, source):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, caregiver, status=source)

    assert act(client, booking, caregiver, action).status_code == 409
    db.refresh(booking)
    assert booking.status == source
