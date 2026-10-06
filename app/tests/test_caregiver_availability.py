"""C3 item 3: POST /caregiver/availability is the availability contract."""

from datetime import timedelta

import pytest

from app.db.models.booking import BookingStatus, CaregiverLocation
from app.services.dispatch import find_candidates, start_dispatch
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient, refresh_headers
from app.tests.test_dispatch import NOW, fresh, make_candidate, notified_ids, paid_booking

URL = "/api/v1/caregiver/availability"


def location(db, cg):
    db.expire_all()
    return db.query(CaregiverLocation).filter_by(caregiver_id=cg.id).first()


def test_requires_authentication(db, client):
    cg = make_caregiver(db)
    assert client.post(URL, json={"is_available": True}).status_code == 401
    assert client.post(URL, json={"is_available": True}, headers=refresh_headers(cg.user)).status_code == 401


def test_a_patient_is_forbidden(db, client):
    patient = make_patient(db)
    r = client.post(URL, json={"is_available": True}, headers=auth_headers(patient.user))
    assert r.status_code == 403
    assert r.json()["detail"] == "Access denied. Caregiver account required."


@pytest.mark.parametrize("status", ["incomplete", "under_review", "changes_requested", "rejected", "suspended"])
@pytest.mark.parametrize("flag", [True, False])
def test_only_approved_caregivers_may_change_availability(db, client, status, flag):
    cg = make_caregiver(db, approved=False, profile_status=status)
    r = client.post(URL, json={"is_available": flag}, headers=auth_headers(cg.user))
    assert r.status_code == 403
    assert r.json()["detail"] == "Your account is not approved to change availability"
    assert location(db, cg) is None


@pytest.mark.parametrize("body", [{}, {"is_available": "maybe"}, {"is_available": None}])
def test_body_is_validated(db, client, body):
    cg = make_caregiver(db)
    assert client.post(URL, json=body, headers=auth_headers(cg.user)).status_code == 422


def test_going_available_needs_a_location_first(db, client):
    cg = make_caregiver(db)
    r = client.post(URL, json={"is_available": True}, headers=auth_headers(cg.user))
    assert r.status_code == 409
    assert r.json()["detail"] == "Share your location before going available"
    assert location(db, cg) is None


def test_going_unavailable_without_a_location_is_a_harmless_no_op(db, client):
    cg = make_caregiver(db)
    r = client.post(URL, json={"is_available": False}, headers=auth_headers(cg.user))
    assert r.status_code == 200
    assert r.json() == {"is_available": False}


def test_toggle_updates_the_flag_and_nothing_else(db, client):
    cg = make_candidate(db, age_minutes=7)
    before = location(db, cg)
    stamp, lat = before.updated_at, before.latitude

    r = client.post(URL, json={"is_available": False}, headers=auth_headers(cg.user))
    assert r.status_code == 200 and r.json() == {"is_available": False}
    after = location(db, cg)
    assert after.is_available is False
    # a toggle must not make a stale position look fresh to the matcher
    assert after.updated_at == stamp and after.latitude == lat

    r = client.post(URL, json={"is_available": True}, headers=auth_headers(cg.user))
    assert r.json() == {"is_available": True}
    assert location(db, cg).is_available is True


def test_toggle_is_idempotent(db, client):
    cg = make_candidate(db)
    for _ in range(2):
        r = client.post(URL, json={"is_available": False}, headers=auth_headers(cg.user))
        assert r.status_code == 200
    assert location(db, cg).is_available is False


def test_going_unavailable_stops_new_offers_and_going_available_resumes_them(db, client):
    cg = make_candidate(db)
    assert [c["caregiver_id"] for c in find_candidates(db, paid_booking(db), NOW)] == [cg.id]

    client.post(URL, json={"is_available": False}, headers=auth_headers(cg.user))
    booking = paid_booking(db)
    assert find_candidates(db, booking, NOW) == []
    start_dispatch(db, booking.id, NOW)
    assert notified_ids(db, booking) == []

    client.post(URL, json={"is_available": True}, headers=auth_headers(cg.user))
    again = paid_booking(db)
    start_dispatch(db, again.id, NOW + timedelta(minutes=1))
    assert notified_ids(db, again) == [cg.id]


def test_accepted_bookings_are_unaffected_by_going_unavailable(db, client):
    patient = make_patient(db)
    cg = make_candidate(db)
    booking = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, dispatch_state="matched")

    client.post(URL, json={"is_available": False}, headers=auth_headers(cg.user))

    b = fresh(db, booking)
    assert b.status == BookingStatus.ACCEPTED and b.caregiver_id == cg.id and b.dispatch_state == "matched"


def test_an_offer_already_sent_can_still_be_accepted_after_going_unavailable(db, client):
    cg = make_candidate(db)
    booking = paid_booking(db)
    start_dispatch(db, booking.id, NOW)
    client.post(URL, json={"is_available": False}, headers=auth_headers(cg.user))

    r = client.post(f"/api/v1/booking/caregiver/action/{booking.id}", json={"action": "accept"},
                    headers=auth_headers(cg.user))
    assert r.status_code == 200
