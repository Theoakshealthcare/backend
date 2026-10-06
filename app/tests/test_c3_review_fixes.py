"""Fixes from the C3 security review."""

from datetime import datetime, timedelta

from app.db.models.booking import BookingNotification, BookingStatus, PaymentStatus
from app.db.models.booking import CaregiverLocation
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

PING = "/api/v1/booking/caregiver/location"


# --- 1. the offer view must not expose the caregiver who accepted -----------------------


def test_offer_view_hides_the_accepting_caregivers_identity_and_location(db, client):
    winner, other = make_caregiver(db), make_caregiver(db)
    patient = make_patient(db)
    booking = make_booking(db, patient, status=BookingStatus.PENDING, payment_status=PaymentStatus.PAID)
    for cg in (winner, other):
        db.add(BookingNotification(booking_id=booking.id, caregiver_id=cg.id, title="t", message="m",
                                   notification_type="new_booking"))
    db.add(CaregiverLocation(caregiver_id=winner.id, latitude=6.501234, longitude=3.401234,
                             is_available=True, is_active=True))
    db.commit()
    assert client.post(f"/api/v1/booking/caregiver/action/{booking.id}", json={"action": "accept"},
                       headers=auth_headers(winner.user)).status_code == 200

    item = client.get("/api/v1/booking/caregiver/notifications",
                      headers=auth_headers(other.user)).json()[0]["booking"]
    assert item["caregiver_details"] is None
    assert item["caregiver_id"] is None
    assert "6.501234" not in str(item) and winner.full_name not in str(item)


# --- 2. a location ping never changes availability unless it says so --------------------


def _ping(client, cg, **extra):
    return client.post(PING, json={"latitude": 6.45, "longitude": 3.47, **extra}, headers=auth_headers(cg.user))


def _available(db, cg):
    db.expire_all()
    return db.query(CaregiverLocation).filter(CaregiverLocation.caregiver_id == cg.id).one().is_available


def test_first_ping_without_the_flag_does_not_make_a_caregiver_available(db, client):
    cg = make_caregiver(db)
    assert _ping(client, cg).status_code == 200
    assert _available(db, cg) is False


def test_ping_without_the_flag_keeps_an_unavailable_caregiver_unavailable(db, client):
    cg = make_caregiver(db)
    _ping(client, cg)
    assert client.post("/api/v1/caregiver/availability", json={"is_available": False},
                       headers=auth_headers(cg.user)).status_code == 200
    _ping(client, cg)
    assert _available(db, cg) is False


def test_ping_without_the_flag_keeps_an_available_caregiver_available(db, client):
    cg = make_caregiver(db)
    _ping(client, cg)
    assert client.post("/api/v1/caregiver/availability", json={"is_available": True},
                       headers=auth_headers(cg.user)).status_code == 200
    _ping(client, cg)
    assert _available(db, cg) is True


def test_explicit_flag_on_a_ping_still_applies(db, client):
    cg = make_caregiver(db)
    _ping(client, cg, is_available=True)
    assert _available(db, cg) is True
    _ping(client, cg, is_available=False)
    assert _available(db, cg) is False


# --- 3. suspension and visits already under way -----------------------------------------


def _assigned(db, cg, status):
    start = datetime.utcnow() + timedelta(hours=2)
    return make_booking(db, make_patient(db), cg, status=status, payment_status=PaymentStatus.PAID,
                        scheduled_date=start, scheduled_end_date=start + timedelta(hours=2))


def _suspend(db, cg):
    cg.profile_status = "suspended"
    cg.suspended_at = datetime.utcnow()
    db.commit()


def test_suspended_caregiver_cannot_go_on_the_way_or_start(db, client):
    cg = make_caregiver(db)
    accepted = _assigned(db, cg, BookingStatus.ACCEPTED)
    en_route = _assigned(db, cg, BookingStatus.EN_ROUTE)
    _suspend(db, cg)
    r = client.post(f"/api/v1/booking/caregiver/action/{accepted.id}", json={"action": "on_my_way"},
                    headers=auth_headers(cg.user))
    assert r.status_code == 403
    r = client.post(f"/api/v1/booking/caregiver/action/{en_route.id}",
                    json={"action": "start", "visit_code": "0000"}, headers=auth_headers(cg.user))
    assert r.status_code == 403


def test_suspended_caregiver_can_still_complete_a_visit_in_progress(db, client):
    cg = make_caregiver(db)
    visit = _assigned(db, cg, BookingStatus.IN_PROGRESS)
    _suspend(db, cg)
    r = client.post(f"/api/v1/booking/caregiver/action/{visit.id}", json={"action": "complete"},
                    headers=auth_headers(cg.user))
    assert r.status_code == 200


def test_suspend_lists_en_route_bookings_for_reassignment(db, client):
    from app.tests.factories import make_admin

    cg = make_caregiver(db)
    accepted = _assigned(db, cg, BookingStatus.ACCEPTED)
    en_route = _assigned(db, cg, BookingStatus.EN_ROUTE)
    admin = make_admin(db)
    r = client.post(f"/api/v1/admin/caregivers/{cg.id}/suspend", json={"reason": "test"},
                    headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    assert set(r.json()["affected_bookings"]) == {str(accepted.id), str(en_route.id)}
