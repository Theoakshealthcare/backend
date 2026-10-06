"""W2: the shared booking rules (services, cities, slots, times, money) and the strict pricing helper."""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.channels.whatsapp import booking_rules as rules
from app.core.config import settings
from app.services.pricing import DEFAULT_MULTIPLIER, compute_price, price_service, service_multiplier

# 2026-10-06 12:00 UTC = 13:00 in Lagos
NOW = datetime(2026, 10, 6, 12, 0, 0)


def prices(**kw):
    base = dict(base_fee_kobo=1_000_000, platform_fee_percent=10.0, surge_pricing=False, surge_multiplier=1.0,
                discount=0.0, multipliers={"nurse": 1.0, "caregiver": 0.8})
    base.update(kw)
    return SimpleNamespace(**base)


# --- pricing: one multiplier lookup, defaulting to 1.0, shared by both channels ---------------------
def test_an_explicit_multiplier_prices_like_compute_price():
    got = price_service(prices(), "nurse", 4)
    assert got == compute_price(base_fee_kobo=1_000_000, quantity=4, multiplier=1.0, platform_fee_percent=10.0)
    assert (got.service_fee_kobo, got.platform_fee_kobo, got.total_kobo) == (4_000_000, 400_000, 4_400_000)


@pytest.mark.parametrize("multipliers", [None, {}, {"caregiver": 0.8}, {"nurse": None}, {"nurse": 0}, {"nurse": -1},
                                         {"nurse": "1.0"}, {"nurse": True}])
def test_a_missing_or_unusable_multiplier_defaults_to_one_in_both_channels(multipliers, caplog):
    row = prices(multipliers=multipliers)
    with caplog.at_level("WARNING", logger="app.services.pricing"):
        assert service_multiplier(row, "nurse") == DEFAULT_MULTIPLIER == 1.0
    assert price_service(row, "nurse", 2) == compute_price(base_fee_kobo=1_000_000, quantity=2, multiplier=1.0,
                                                           platform_fee_percent=10.0)


def test_using_the_default_is_flagged_for_ops_without_personal_data(caplog):
    with caplog.at_level("WARNING", logger="app.services.pricing"):
        service_multiplier(prices(multipliers={}), "nurse", channel="whatsapp")
    lines = [r.getMessage() for r in caplog.records if r.name == "app.services.pricing"]
    assert len(lines) == 1 and "service=nurse" in lines[0] and "channel=whatsapp" in lines[0]


def test_a_configured_multiplier_is_used_and_not_flagged(caplog):
    with caplog.at_level("WARNING", logger="app.services.pricing"):
        assert service_multiplier(prices(), "caregiver") == 0.8
    assert not [r for r in caplog.records if r.name == "app.services.pricing"]


def test_every_candidate_service_is_priceable_with_no_multipliers_configured():
    row = prices(multipliers=None)
    assert [price_service(row, s, 1).total_kobo for s in rules.SERVICE_ORDER] == [1_100_000] * len(rules.SERVICE_ORDER)


def test_surge_and_discount_follow_the_same_rules_as_the_app():
    row = prices(surge_pricing=True, surge_multiplier=1.5, discount=10.0)
    assert price_service(row, "nurse", 4) == compute_price(
        base_fee_kobo=1_000_000, quantity=4, multiplier=1.0, surge_multiplier=1.5, discount_percent=10.0,
        platform_fee_percent=10.0)
    # surge switched off: the stored multiplier is ignored
    assert price_service(prices(surge_pricing=False, surge_multiplier=2.0), "nurse", 4).surge_adjustment_kobo == 0


# --- cities and durations -------------------------------------------------------------------------------
def test_the_city_list_is_a_setting_and_matching_ignores_case_and_spaces():
    assert rules.service_cities() == ["Lagos", "Abuja"]
    assert rules.canonical_city("  lagos ") == "Lagos" and rules.canonical_city("ABUJA") == "Abuja"
    assert rules.canonical_city("Port Harcourt") is None and rules.canonical_city(None) is None
    assert rules.canonical_city(5) is None


def test_the_per_service_cap_is_a_setting_with_a_default(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_SERVICE_MAX_HOURS", "nurse:6, caregiver:24,bogus:x,doctor:99")
    assert rules.max_hours("nurse") == 6 and rules.max_hours("caregiver") == 24
    assert rules.max_hours("doctor") == 24  # never above the 24 h the booking schema allows
    assert rules.max_hours("physiotherapist") == settings.WHATSAPP_DEFAULT_MAX_HOURS


@pytest.mark.parametrize("lat,lng,ok", [(6.45, 3.47, True), (9.07, 7.39, True), (51.5, -0.12, False), (0, 0, False),
                                        ("6", "3", False), (None, 3.4, False), (True, True, False)])
def test_a_pin_must_be_in_nigeria(lat, lng, ok):
    assert rules.pin_is_plausible(lat, lng) is ok


# --- time -----------------------------------------------------------------------------------------------
def test_lagos_is_one_hour_ahead_of_utc_both_ways():
    assert rules.to_lagos(NOW).hour == 13
    assert rules.lagos_to_utc(date(2026, 10, 10), "10:00") == datetime(2026, 10, 10, 9, 0)
    assert rules.today_lagos(datetime(2026, 10, 6, 23, 30)) == date(2026, 10, 7)  # 00:30 in Lagos already


def test_the_booking_window_is_today_to_thirty_days():
    assert rules.date_range(NOW) == (date(2026, 10, 6), date(2026, 11, 5))


def test_slots_respect_the_lead_time_and_opening_hours_today():
    # 13:00 Lagos + 2 h lead: the first slot is 15:00
    assert rules.slots_for(date(2026, 10, 6), NOW)[0] == "15:00"
    assert rules.slots_for(date(2026, 10, 6), NOW)[-1] == "20:00"
    assert rules.slots_for(date(2026, 10, 7), NOW)[:2] == ["07:00", "08:00"]
    assert len(rules.slots_for(date(2026, 10, 7), NOW)) == 14


def test_no_slots_outside_the_window_and_the_first_bookable_day_skips_a_full_day():
    assert rules.slots_for(date(2026, 10, 5), NOW) == [] and rules.slots_for(date(2026, 11, 6), NOW) == []
    late = datetime(2026, 10, 6, 19, 30)  # 20:30 in Lagos: nothing left today
    assert rules.slots_for(date(2026, 10, 6), late) == [] and rules.first_bookable_day(late) == date(2026, 10, 7)


def test_slot_titles_are_twelve_hour():
    assert [rules.slot_title(s) for s in ("07:00", "12:00", "13:00", "20:00")] == \
        ["7:00 AM", "12:00 PM", "1:00 PM", "8:00 PM"]


@pytest.mark.parametrize("raw,expected", [("2026-10-06", date(2026, 10, 6)), ("2026-10-06T00:00:00", date(2026, 10, 6)),
                                          ("nope", None), (None, None), (5, None)])
def test_parse_day(raw, expected):
    assert rules.parse_day(raw) == expected


# --- display --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kobo,text", [(4_400_000, "₦44,000"), (4_400_050, "₦44,000.50"), (50, "₦0.50"), (0, "₦0"),
                                       (123_456_700, "₦1,234,567")])
def test_naira_formatting(kobo, text):
    assert rules.naira(kobo) == text


def test_when_is_shown_in_lagos_time_with_the_end_of_the_visit():
    assert rules.format_when(datetime(2026, 10, 4, 9, 0), 4) == "Sun 4 Oct, 10:00–14:00"
    assert rules.format_when(datetime(2026, 10, 4, 21, 0), 4) == "Sun 4 Oct, 22:00–02:00"


@pytest.mark.parametrize("value", [10.01, 25, 1e9, float("inf")])
def test_an_absurd_multiplier_falls_back_to_the_default_with_a_warning(value, caplog):
    with caplog.at_level("WARNING", logger="app.services.pricing"):
        assert service_multiplier(prices(multipliers={"nurse": value}), "nurse") == 1.0
    assert [r for r in caplog.records if r.name == "app.services.pricing"]


def test_a_multiplier_of_exactly_ten_is_allowed():
    assert service_multiplier(prices(multipliers={"nurse": 10}), "nurse") == 10


@pytest.mark.parametrize("city,spelling,ok", [
    ("Lagos", "Lagos", True), ("Lagos", "Lagos State", True), ("Lagos", " lagos ", True),
    ("Lagos", "Ogun State", False), ("Lagos", None, False), ("Lagos", "", False),
    ("Abuja", "Federal Capital Territory", True), ("Abuja", "FCT", True), ("Abuja", "Abuja Federal Capital Territory", True),
    ("Abuja", "Nasarawa State", False), ("Abuja", "Niger State", False), ("Kano", "Kano", False),
])
def test_the_reported_state_must_match_the_served_city(city, spelling, ok):
    assert rules.state_matches(city, spelling) is ok
