"""Phase 2, slice 2: bookings in minutes. Request resolution, the catalogue's length rule, proration and its rounding
rule (with seeded property tests), the response fields, the short-visit rule in minutes, and the other views."""

import json
import random
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models.booking import Booking, BookingStatus
from app.db.models.service_catalog import ServiceCatalog
from app.services import earnings
from app.services.pricing import compute_price, price_service
from app.tests.booking_fixtures import make_prices
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

API = "/api/v1/booking"
START = "2030-01-01T10:00:00"


def preview_body(**extra):
    return {"service_type": "nurse", **extra}


def create_body(**extra):
    return dict(service_type="nurse", assistance_type="x", scheduled_date=START, patient_address="1 Test Close",
                patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos", patient_state="Lagos",
                booking_for="myself", **extra)


# --- the model's derived hours -------------------------------------------------------------------------------------------
def test_duration_hours_is_derived_from_minutes_and_stays_an_int_when_whole():
    assert Booking(duration_minutes=240).duration_hours == 4 and isinstance(Booking(duration_minutes=240).duration_hours, int)
    assert Booking(duration_minutes=90).duration_hours == 1.5
    assert Booking(duration_minutes=30).duration_hours == 0.5
    old = Booking(duration_hours=2)  # old callers keep working
    assert old.duration_minutes == 120
    assert Booking().duration_hours is None


# --- request resolution --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sent,minutes,hours", [
    ({"duration_minutes": 90}, 90, 1.5),
    ({"duration_hours": 4}, 240, 4),
    ({"duration_hours": 1.5}, 90, 1.5),
    ({"duration_minutes": 120, "duration_hours": 2}, 120, 2),
    ({"duration_minutes": 60, "duration_hours": 1.0}, 60, 1),
])
def test_preview_accepts_minutes_or_hours_and_returns_both(db, client, sent, minutes, hours):
    make_prices(db, multipliers=None)
    r = client.post(f"{API}/preview", json=preview_body(**sent))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duration_minutes"] == minutes and body["duration_hours"] == hours


def test_whole_hours_come_back_as_a_json_integer_for_the_existing_apps(db, client):
    make_prices(db, multipliers=None)
    raw = client.post(f"{API}/preview", json=preview_body(duration_hours=4)).text
    assert '"duration_hours":4,' in raw.replace(" ", "")
    raw = client.post(f"{API}/preview", json=preview_body(duration_minutes=90)).text
    assert '"duration_hours":1.5,' in raw.replace(" ", "")


@pytest.mark.parametrize("sent", [
    {}, {"duration_minutes": 60, "duration_hours": 2}, {"duration_hours": 1.001}, {"duration_hours": "soon"},
    {"duration_minutes": "x"}, {"duration_minutes": 60.5},
])
def test_preview_refuses_a_missing_conflicting_or_malformed_length(db, client, sent):
    make_prices(db, multipliers=None)
    r = client.post(f"{API}/preview", json=preview_body(**sent))
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # a malformed request is FastAPI's list shape, not invalid_duration


# --- the catalogue's length rule -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("minutes", [0, -60, 15, 45, 61, 1470, 1500, 30])
def test_preview_and_create_refuse_a_length_the_service_does_not_allow(db, client, fake_paystack, minutes):
    make_prices(db, multipliers=None)
    r = client.post(f"{API}/preview", json=preview_body(duration_minutes=minutes))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_duration"
    assert (detail["min_minutes"], detail["step_minutes"], detail["max_minutes"]) == (60, 30, 1440)
    assert detail["allowed_minutes"][:4] == [60, 90, 120, 150] and detail["allowed_minutes"][-1] == 1440
    assert len(detail["allowed_minutes"]) == 47
    assert "60" in detail["message"] and "1440" in detail["message"]
    patient = make_patient(db)
    r = client.post(f"{API}/create", json=create_body(duration_minutes=minutes), headers=auth_headers(patient.user))
    assert r.status_code == 422 and r.json()["detail"] == detail
    assert db.query(Booking).count() == 0 and fake_paystack.initialised == []


def test_the_old_hours_field_gets_the_same_rule(db, client):
    make_prices(db, multipliers=None)
    r = client.post(f"{API}/preview", json=preview_body(duration_hours=0))
    assert r.json()["detail"]["code"] == "invalid_duration"
    r = client.post(f"{API}/preview", json=preview_body(duration_hours=25))
    assert r.json()["detail"]["code"] == "invalid_duration"
    assert client.post(f"{API}/preview", json=preview_body(duration_hours=24)).status_code == 200


def test_a_service_with_its_own_shortest_booking_allows_30_minutes(db, client):
    make_prices(db, multipliers={"nurse": 1.0, "nutritionist": 0.9})  # also enables nutritionist
    assert client.post(f"{API}/preview", json={"service_type": "nutritionist", "duration_minutes": 30}).status_code == 200
    r = client.post(f"{API}/preview", json={"service_type": "nurse", "duration_minutes": 30})
    assert r.status_code == 422 and r.json()["detail"]["min_minutes"] == 60


def test_the_admin_can_change_the_rule_and_it_takes_effect(db, client):
    make_prices(db, multipliers=None)
    admin = make_admin(db, roles=("super_admin",))
    body = dict(enabled=True, display_name="Nurse visit", mode="home", min_minutes=120, step_minutes=60, max_minutes=240,
                sort_order=10)
    assert client.put("/api/v1/admin/service-catalog/nurse", json=body, headers=auth_headers(admin)).status_code == 200
    ok = client.post(f"{API}/preview", json=preview_body(duration_minutes=180))
    assert ok.status_code == 200
    bad = client.post(f"{API}/preview", json=preview_body(duration_minutes=90))
    assert bad.json()["detail"]["allowed_minutes"] == [120, 180, 240]


def test_a_hidden_service_is_refused_before_its_length_is_looked_at(db, client):
    make_prices(db, multipliers=None)
    r = client.post(f"{API}/preview", json={"service_type": "doctor", "duration_minutes": 45})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "service_unavailable"


# --- prices -------------------------------------------------------------------------------------------------------------
def test_preview_prorates_by_minutes_and_matches_the_old_hours_price(db, client):
    make_prices(db, multipliers=None, base_fee_kobo=1_000_000)  # 10,000.00 naira an hour, platform fee 10 %
    hour = client.post(f"{API}/preview", json=preview_body(duration_hours=1)).json()
    half_more = client.post(f"{API}/preview", json=preview_body(duration_minutes=90)).json()
    four = client.post(f"{API}/preview", json=preview_body(duration_hours=4)).json()
    assert hour["service_fee"] == 10000.0 and hour["platform_fee"] == 1000.0 and hour["total_amount"] == 11000.0
    assert half_more["service_fee"] == 15000.0 and half_more["total_amount"] == 16500.0
    assert four["service_fee"] == 40000.0  # exactly what 4 hours always cost


def test_create_stores_minutes_the_end_time_and_the_previewed_total(db, client, fake_paystack):
    make_prices(db, multipliers=None, base_fee_kobo=1_000_000)
    patient = make_patient(db)
    preview = client.post(f"{API}/preview", json=preview_body(duration_minutes=90)).json()
    r = client.post(f"{API}/create", json=create_body(duration_minutes=90), headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duration_minutes"] == 90 and body["duration_hours"] == 1.5
    assert body["total_amount"] == preview["total_amount"] == 16500.0
    booking = db.query(Booking).one()
    assert booking.duration_minutes == 90 and booking.duration_hours == 1.5
    assert booking.scheduled_end_date - booking.scheduled_date == timedelta(minutes=90)
    assert booking.total_amount_kobo == 1_650_000 and fake_paystack.initialised[0]["amount"] == 1_650_000


def test_create_with_the_old_hours_field_still_works_and_is_stored_in_minutes(db, client, fake_paystack):
    make_prices(db, multipliers=None)
    patient = make_patient(db)
    r = client.post(f"{API}/create", json=create_body(duration_hours=3), headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.json()["duration_hours"] == 3 and r.json()["duration_minutes"] == 180
    booking = db.query(Booking).one()
    assert booking.duration_minutes == 180 and booking.scheduled_end_date - booking.scheduled_date == timedelta(hours=3)
    got = client.get(f"{API}/booking/{booking.id}", headers=auth_headers(patient.user)).json()
    assert got["duration_minutes"] == 180 and got["duration_hours"] == 3
    listed = client.get(f"{API}/patient/bookings", headers=auth_headers(patient.user)).json()
    assert listed[0]["duration_minutes"] == 180 and listed[0]["duration_hours"] == 3


# --- the rounding rule and the properties --------------------------------------------------------------------------------
def test_rounding_rule_one_half_up_rounding_from_the_exact_product():
    # 333 kobo an hour, 30 minutes: exactly 166.5 -> 167 (half up)
    p = compute_price(base_fee_kobo=333, minutes=30, platform_fee_percent=0)
    assert (p.base_calculation_kobo, p.service_fee_kobo, p.total_kobo) == (167, 167, 167)
    # 1001 kobo, 90 minutes: 1501.5 -> 1502
    assert compute_price(base_fee_kobo=1001, minutes=90, platform_fee_percent=0).service_fee_kobo == 1502
    # one rounding at the end, not per step: 100 kobo x 1/3-hour-ish values do not accumulate error
    p = compute_price(base_fee_kobo=1000, minutes=45, multiplier=1.0, surge_multiplier=1.5, discount_percent=10,
                      platform_fee_percent=10)
    exact_service = Decimal(1000) * 45 / 60 * Decimal("1.5") * Decimal("0.9")  # 1012.5
    assert p.service_fee_kobo == 1013 and exact_service == Decimal("1012.5")
    assert p.platform_fee_kobo == 101 and p.total_kobo == 1013 + 101  # platform: 101.25 -> 101
    assert p.total_kobo == p.service_fee_kobo + p.platform_fee_kobo


def test_the_minutes_path_equals_the_hours_path_for_whole_hours():
    rng = random.Random(20261108)
    for _ in range(300):
        kw = dict(base_fee_kobo=rng.randint(1, 5_000_000), multiplier=round(rng.uniform(0.1, 3), 2),
                  surge_multiplier=rng.choice([None, 1.2, 1.5]), discount_percent=rng.choice([None, 5, 12.5]),
                  platform_fee_percent=rng.choice([0, 10, 12.5]))
        hours = rng.randint(1, 24)
        assert compute_price(quantity=hours, **kw) == compute_price(minutes=hours * 60, **kw)


def _random_settings(rng):
    return dict(base_fee_kobo=rng.choice([1, 2, 50, 333, 9_999, 500_000, 1_000_000, 123_457, 100_000_000]),
                multiplier=rng.choice([0.01, 0.1, 0.33, 0.8, 1.0, 1.7, 2.0, 10.0]),
                surge_multiplier=rng.choice([None, 1.0, 1.1, 1.5, 5.0]),
                discount_percent=rng.choice([None, 0, 5, 10, 33.3, 100]),
                platform_fee_percent=rng.choice([0, 3.5, 10, 12.5, 100]))


def test_property_price_is_monotonic_in_minutes():
    rng = random.Random(1)
    for _ in range(400):
        kw = _random_settings(rng)
        previous = None
        for minutes in range(15, 1441, 15):
            p = compute_price(minutes=minutes, **kw)
            if previous is not None:
                assert p.service_fee_kobo >= previous.service_fee_kobo, (kw, minutes)
                assert p.total_kobo >= previous.total_kobo, (kw, minutes)
            previous = p


def test_property_the_parts_never_exceed_the_total():
    """The caregiver's share of the service fee plus the platform fee can never be more than what the family pays."""
    rng = random.Random(2)
    for _ in range(600):
        kw = _random_settings(rng)
        for minutes in rng.sample(range(30, 1441, 30), 6):
            p = compute_price(minutes=minutes, **kw)
            assert p.total_kobo == p.service_fee_kobo + p.platform_fee_kobo
            for percent in (0, 50, 80, 100):
                assert earnings.share(p.service_fee_kobo, percent) + p.platform_fee_kobo <= p.total_kobo, (kw, minutes, percent)


def test_property_a_30_minute_booking_never_prices_at_zero_or_above_its_hour():
    rng = random.Random(3)
    for _ in range(600):
        kw = _random_settings(rng)
        kw["discount_percent"] = rng.choice([None, 0, 5, 33.3])  # a 100 % discount is a free visit, on purpose
        half, hour = compute_price(minutes=30, **kw), compute_price(minutes=60, **kw)
        assert half.service_fee_kobo >= 1 and half.total_kobo >= 1, kw
        assert half.service_fee_kobo <= hour.service_fee_kobo and half.total_kobo <= hour.total_kobo, kw


def test_a_full_discount_is_still_a_free_visit_not_one_kobo():
    p = compute_price(base_fee_kobo=500_000, minutes=30, discount_percent=100, platform_fee_percent=0)
    assert p.service_fee_kobo == 0


def test_a_sub_kobo_price_is_floored_at_one_kobo_rather_than_nothing():
    assert compute_price(base_fee_kobo=1, multiplier=0.01, minutes=30, platform_fee_percent=0).service_fee_kobo == 1


def test_price_service_takes_minutes_for_the_whatsapp_and_flow_callers():
    class S:
        base_fee_kobo, multipliers, surge_pricing, surge_multiplier, discount, platform_fee_percent = (
            1_000_000, {"nurse": 1.0}, False, 1.0, 0.0, 10.0)
    assert price_service(S, "nurse", 3) == price_service(S, "nurse", minutes=180)


# --- short-visit rule uses the booked minutes ----------------------------------------------------------------------------
@pytest.mark.parametrize("booked_minutes,on_site_minutes,short", [
    (90, 44, True), (90, 45, False),            # 50 % of 90 minutes is 45
    (30, 14, True), (30, 15, False),
    (240, 119, True), (240, 120, False),
])
def test_short_visit_is_judged_against_booked_minutes(db, booked_minutes, on_site_minutes, short):
    cg = make_caregiver(db)
    start = datetime(2026, 11, 7, 10, 0)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, duration_minutes=booked_minutes,
                           actual_start_time=start, actual_end_time=start + timedelta(minutes=on_site_minutes))
    assert earnings.is_short_visit(booking, 50) is short


def test_the_old_hours_keyword_still_builds_a_booking_in_tests_and_scripts(db):
    booking = make_booking(db, make_patient(db), duration_hours=2)
    assert booking.duration_minutes == 120


# --- the other views -----------------------------------------------------------------------------------------------------
def test_caregiver_and_admin_views_carry_both_fields(db, client):
    cg = make_caregiver(db)
    patient = make_patient(db)
    booking = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, duration_minutes=90)
    admin = make_admin(db, roles=("super_admin",))
    detail = client.get(f"/api/v1/admin/bookings/{booking.id}", headers=auth_headers(admin)).json()
    assert detail["duration_minutes"] == 90 and detail["duration_hours"] == 1.5
    mine = client.get(f"{API}/caregiver/bookings", headers=auth_headers(cg.user)).json()
    assert mine and mine[0]["duration_minutes"] == 90 and mine[0]["duration_hours"] == 1.5
    home = client.get("/api/v1/caregiver/home", headers=auth_headers(cg.user))
    if home.status_code == 200:
        text = json.dumps(home.json())
        assert '"duration_minutes": 90' in text


# --- WhatsApp stays in whole hours but obeys the catalogue ---------------------------------------------------------------
def test_flow_duration_options_follow_the_catalogue_minimum_and_step(db):
    from types import SimpleNamespace

    from app.channels.whatsapp.flows.service import FlowService

    make_prices(db, multipliers=None)
    flow = FlowService.__new__(FlowService)
    flow.db = db
    quote = SimpleNamespace(payload={"service": "nurse"})

    def options():
        return [d["id"] for d in flow._render_details(quote).body["data"]["durations"]]

    assert options()[:3] == ["1", "2", "3"]
    db.query(ServiceCatalog).filter(ServiceCatalog.service_type == "nurse").update({"min_minutes": 120, "max_minutes": 300})
    db.commit()
    assert options() == ["2", "3", "4", "5"]
