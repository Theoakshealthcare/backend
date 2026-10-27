"""C3 item 5: GET /caregiver/home, the one call behind the home screen."""

from datetime import datetime, timedelta

import pytest

from app.db.models.booking import BookingNotification, BookingStatus, CaregiverLocation, PaymentStatus
from app.services.caregiver_home import build_home
from app.tests.factories import (
    auth_headers,
    make_booking,
    make_caregiver,
    make_caregiver_wallet,
    make_patient,
    refresh_headers,
)

URL = "/api/v1/caregiver/home"
HOME_KEYS = {"profile_status", "is_available", "location_updated_at", "today_visits", "upcoming_visits",
             "open_offers", "wallet_balance_naira", "currency", "can_author_care_plans"}


def offer(db, booking, cg, **kw):
    n = BookingNotification(booking_id=booking.id, caregiver_id=cg.id, title="New Booking Request",
                            message="x", notification_type="new_booking", **kw)
    db.add(n)
    db.commit()
    return n


def get(client, cg):
    r = client.get(URL, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    return r.json()


# --- auth -----------------------------------------------------------------------------


def test_requires_authentication_and_a_caregiver(db, client):
    cg, patient = make_caregiver(db), make_patient(db)
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=refresh_headers(cg.user)).status_code == 401
    r = client.get(URL, headers=auth_headers(patient.user))
    assert r.status_code == 403
    assert r.json()["detail"] == "Access denied. Caregiver account required."


# --- shape ----------------------------------------------------------------------------


def test_a_brand_new_caregiver_gets_empty_but_complete_data(db, client):
    cg = make_caregiver(db, approved=False, profile_status="incomplete")
    body = get(client, cg)
    assert set(body) == HOME_KEYS
    assert body == {
        "profile_status": "incomplete",
        "is_available": False,
        "location_updated_at": None,
        "today_visits": [],
        "upcoming_visits": [],
        "open_offers": [],
        "wallet_balance_naira": 0.0,
        "currency": "NGN",
        "can_author_care_plans": False,
    }


def test_wallet_balance_is_converted_from_kobo_to_naira(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=123_456)
    body = get(client, cg)
    assert body["wallet_balance_naira"] == 1234.56
    assert isinstance(body["wallet_balance_naira"], float)


def test_availability_follows_the_location_row(db, client):
    cg = make_caregiver(db)
    stamp = datetime.utcnow() - timedelta(minutes=3)
    db.add(CaregiverLocation(caregiver_id=cg.id, latitude=6.4, longitude=3.4, is_available=True, updated_at=stamp))
    db.commit()
    body = get(client, cg)
    assert body["is_available"] is True
    assert body["location_updated_at"].startswith(stamp.isoformat()[:19])

    client.post("/api/v1/caregiver/availability", json={"is_available": False}, headers=auth_headers(cg.user))
    assert get(client, cg)["is_available"] is False


# --- visits ---------------------------------------------------------------------------


def test_today_and_upcoming_visits(db, client):
    patient, cg = make_patient(db, full_name="Placeholder Patient"), make_caregiver(db)
    now = datetime.utcnow()
    today = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, scheduled_date=now,
                         scheduled_end_date=now + timedelta(hours=2), dispatch_state="matched")
    active = make_booking(db, patient, cg, status=BookingStatus.IN_PROGRESS, scheduled_date=now,
                          scheduled_end_date=now + timedelta(hours=2), dispatch_state="matched")
    en_route = make_booking(db, patient, cg, status=BookingStatus.EN_ROUTE, scheduled_date=now,
                            scheduled_end_date=now + timedelta(hours=2), dispatch_state="matched")
    later = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED,
                         scheduled_date=now + timedelta(days=3), scheduled_end_date=now + timedelta(days=3, hours=2))
    far = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED,
                       scheduled_date=now + timedelta(days=9), scheduled_end_date=now + timedelta(days=9, hours=2))

    body = get(client, cg)
    assert {v["id"] for v in body["today_visits"]} == {str(today.id), str(active.id), str(en_route.id)}
    assert [v["id"] for v in body["upcoming_visits"]] == [str(later.id), str(far.id)]  # soonest first

    v = next(v for v in body["today_visits"] if v["id"] == str(en_route.id))
    assert v == {
        "id": str(en_route.id),
        "booking_code": en_route.booking_code,
        "service_type": "nurse",
        "status": "en_route",
        "dispatch_state": "matched",
        "scheduled_date": en_route.scheduled_date.isoformat(),
        "scheduled_end_date": en_route.scheduled_end_date.isoformat(),
        "duration_hours": 4,
        "patient_name": "Placeholder Patient",
        "patient_address": "1 Test Close, Lekki",
        "patient_city": "Lagos",
        "patient_state": "Lagos",
    }
    assert "visit_code" not in str(body)


def test_finished_and_other_peoples_visits_are_not_listed(db, client):
    patient, cg, other = make_patient(db), make_caregiver(db), make_caregiver(db)
    now = datetime.utcnow()
    for st in (BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.DECLINED):
        make_booking(db, patient, cg, status=st, scheduled_date=now, scheduled_end_date=now + timedelta(hours=1))
    make_booking(db, patient, other, status=BookingStatus.ACCEPTED, scheduled_date=now,
                 scheduled_end_date=now + timedelta(hours=1))
    body = get(client, cg)
    assert body["today_visits"] == [] and body["upcoming_visits"] == []


def test_today_is_the_lagos_calendar_day():
    """Fixed clock: 23:30 UTC is already tomorrow in Lagos (UTC+1)."""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        cg = make_caregiver(db)
        patient = make_patient(db)
        now = datetime(2026, 10, 5, 22, 30)  # 23:30 Lagos on the 5th
        same_day = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED,
                                scheduled_date=datetime(2026, 10, 5, 22, 59),  # 23:59 Lagos, 5th
                                scheduled_end_date=datetime(2026, 10, 6, 1, 0))
        next_day = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED,
                                scheduled_date=datetime(2026, 10, 5, 23, 0),  # 00:00 Lagos, 6th
                                scheduled_end_date=datetime(2026, 10, 6, 3, 0))
        overdue = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED,
                               scheduled_date=datetime(2026, 10, 4, 10, 0),
                               scheduled_end_date=datetime(2026, 10, 4, 12, 0))
        home = build_home(db, cg, now=now)
    assert {v["id"] for v in home["today_visits"]} == {same_day.id, overdue.id}
    assert [v["id"] for v in home["upcoming_visits"]] == [next_day.id]


def test_a_visit_already_underway_stays_in_today_even_if_due_later():
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        cg, patient = make_caregiver(db), make_patient(db)
        now = datetime(2026, 10, 5, 10, 0)
        later = datetime(2026, 10, 8, 10, 0)
        early = make_booking(db, patient, cg, status=BookingStatus.EN_ROUTE,
                             scheduled_date=later, scheduled_end_date=later + timedelta(hours=2))
        waiting = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED,
                               scheduled_date=later, scheduled_end_date=later + timedelta(hours=2))
        home = build_home(db, cg, now=now)
    assert [v["id"] for v in home["today_visits"]] == [early.id]
    assert [v["id"] for v in home["upcoming_visits"]] == [waiting.id]


# --- offers ---------------------------------------------------------------------------


def paid_pending(db, patient=None, **kw):
    return make_booking(db, patient or make_patient(db, full_name="Placeholder Patient"),
                        payment_status=PaymentStatus.PAID, dispatch_state="dispatching", **kw)


def test_open_offers_are_minimal_and_carry_no_patient_information(db, client):
    cg = make_caregiver(db)
    b = paid_pending(db, condition="Placeholder-Condition", special_instructions="Gate 0000")
    n = offer(db, b, cg)

    body = get(client, cg)
    assert body["open_offers"] == [{
        "booking_id": str(b.id),
        "service_type": "nurse",
        "scheduled_date": b.scheduled_date.isoformat(),
        "scheduled_end_date": b.scheduled_end_date.isoformat(),
        "duration_hours": 4,
        "area": "Lagos",
        "state": "Lagos",
        "offered_at": n.created_at.isoformat(),
    }]
    text = str(body["open_offers"])
    for secret in ("Placeholder", "Gate", "Test Close", b.booking_code):
        assert secret not in text


def test_offers_exclude_declined_taken_cancelled_and_unpaid_bookings(db, client):
    cg, rival = make_caregiver(db), make_caregiver(db)
    open_ = paid_pending(db)
    offer(db, open_, cg)
    declined = paid_pending(db)
    offer(db, declined, cg, response="declined")
    taken = make_booking(db, make_patient(db), rival, status=BookingStatus.ACCEPTED, dispatch_state="matched")
    offer(db, taken, cg, response="auto_declined")
    taken_no_response = make_booking(db, make_patient(db), rival, status=BookingStatus.ACCEPTED,
                                     dispatch_state="matched")
    offer(db, taken_no_response, cg)
    cancelled = paid_pending(db, status=BookingStatus.CANCELLED)
    offer(db, cancelled, cg)
    unpaid = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    offer(db, unpaid, cg)
    not_mine = paid_pending(db)
    offer(db, not_mine, rival)

    ids = [o["booking_id"] for o in get(client, cg)["open_offers"]]
    assert ids == [str(open_.id)]


def test_unmatched_bookings_still_show_as_offers_because_accept_still_works(db, client):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PAID, dispatch_state="unmatched")
    offer(db, b, cg)
    assert [o["booking_id"] for o in get(client, cg)["open_offers"]] == [str(b.id)]


@pytest.mark.parametrize("status", ["under_review", "suspended"])
def test_caregivers_who_cannot_accept_see_no_offers(db, client, status):
    cg = make_caregiver(db, approved=False, profile_status=status)
    offer(db, paid_pending(db), cg)
    body = get(client, cg)
    assert body["profile_status"] == status and body["open_offers"] == []


def test_newest_offer_first(db, client):
    cg = make_caregiver(db)
    older, newer = paid_pending(db), paid_pending(db)
    offer(db, older, cg, created_at=datetime.utcnow() - timedelta(minutes=5))
    offer(db, newer, cg)
    assert [o["booking_id"] for o in get(client, cg)["open_offers"]] == [str(newer.id), str(older.id)]
