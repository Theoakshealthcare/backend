"""Did the caregiver really set out? A qualifying "on my way" is early enough and followed by real movement."""

from datetime import datetime, timedelta

import pytest

from app.services.onmyway import distance_m, moved_toward, qualifies

START = datetime(2026, 10, 7, 12, 0)
TAP = START - timedelta(minutes=90)
PING = TAP + timedelta(minutes=10)


def test_distance_is_in_metres():
    assert 295 < distance_m(6.4474, 3.4723, 6.4474 + 0.0027, 3.4723) < 305
    assert distance_m(6.4474, 3.4723, 6.4474, 3.4723) == 0


@pytest.mark.parametrize("start, now, expected", [
    (1000, 600, True), (1000, 700, True), (1000, 701, False), (1000, 1000, False), (1000, 1500, False),
])
def test_moved_toward_needs_at_least_the_threshold(start, now, expected):
    assert moved_toward(start, now, min_progress_m=300) is expected


def test_default_threshold_is_300_metres():
    assert moved_toward(1000, 700) and not moved_toward(1000, 750)


def q(**kw):
    args = dict(on_my_way_at=TAP, scheduled_date=START, window_minutes=120, start_distance_m=2000,
                ping_at=PING, ping_distance_m=1000)
    args.update(kw)
    return qualifies(**args)


def test_early_enough_and_moved_qualifies():
    assert q() is True


def test_the_window_is_inclusive_and_earlier_does_not_qualify():
    assert q(on_my_way_at=START - timedelta(minutes=120), ping_at=START - timedelta(minutes=100)) is True
    assert q(on_my_way_at=START - timedelta(minutes=121), ping_at=START - timedelta(minutes=100)) is False


def test_a_tap_with_no_movement_does_not_qualify():
    assert q(ping_distance_m=1900) is False
    assert q(ping_distance_m=2500) is False  # moving away


def test_a_ping_must_come_after_the_tap():
    assert q(ping_at=TAP) is False
    assert q(ping_at=TAP - timedelta(minutes=1)) is False
    assert q(ping_at=None, ping_distance_m=None) is False


def test_without_a_starting_distance_it_cannot_be_shown_and_does_not_qualify():
    assert q(start_distance_m=None) is False


def test_the_window_follows_the_argument():
    assert q(window_minutes=60) is False
    assert q(window_minutes=90) is True
