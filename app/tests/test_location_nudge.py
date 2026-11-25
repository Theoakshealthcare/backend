"""The stale-location nudge: who is pushed "Update your location", when, and how often (explicit ``now`` throughout)."""

from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models.booking import BookingStatus, CaregiverLocation
from app.db.models.push_token import PushToken
from app.services.location_nudge import NUDGE_BODY, NUDGE_TITLE, in_quiet_hours, nudge_stale_locations
from app.tests.factories import make_booking, make_caregiver, make_patient

# 2026-11-25 12:00 UTC is 13:00 in Africa/Lagos (UTC+1): outside the quiet hours.
NOON = datetime(2026, 11, 25, 12, 0)
STALE = timedelta(hours=settings.AVAILABLE_LOCATION_MAX_HOURS, minutes=1)


def stale_caregiver(db, *, age=STALE, available=True, token=True, now=NOON, suspended=False, approved=True, **loc):
    cg = make_caregiver(db, approved=approved, **({"profile_status": "suspended", "suspended_at": now} if suspended else {}))
    db.add(CaregiverLocation(caregiver_id=cg.id, latitude=6.45, longitude=3.47, is_active=True, is_available=available,
                             updated_at=now - age, **loc))
    if token:
        db.add(PushToken(user_id=cg.user_id, token=f"ExponentPushToken[nudge-{cg.id.hex[:8]}]", platform="ios"))
    db.commit()
    return cg


def location_of(db, cg):
    db.expire_all()
    return db.query(CaregiverLocation).filter_by(caregiver_id=cg.id).one()


def test_a_stale_available_caregiver_is_pushed_with_the_agreed_message(db, expo):
    cg = stale_caregiver(db)
    assert nudge_stale_locations(db, NOON) == 1
    msg = expo.messages[0]
    assert (msg["title"], msg["body"], msg["data"]) == (NUDGE_TITLE, NUDGE_BODY, {"type": "location_stale"})
    assert msg["to"].startswith("ExponentPushToken[nudge-")
    assert msg["title"] == "Update your location"
    assert msg["body"] == ("We have not seen your location for a while. Open Oakly so we can keep offering you visits "
                           "near you.")
    assert location_of(db, cg).location_nudged_at == NOON


def test_the_nudge_does_not_make_the_stale_fix_look_fresh(db, expo):
    cg = stale_caregiver(db)
    before = location_of(db, cg).updated_at
    nudge_stale_locations(db, NOON)
    assert location_of(db, cg).updated_at == before


def test_a_fix_inside_the_window_is_not_nudged(db, expo):
    stale_caregiver(db, age=timedelta(hours=settings.AVAILABLE_LOCATION_MAX_HOURS) - timedelta(minutes=1))
    assert nudge_stale_locations(db, NOON) == 0
    assert expo.messages == []


@pytest.mark.parametrize("kw", [{"available": False}, {"token": False}, {"suspended": True}, {"approved": False}])
def test_ineligible_caregivers_are_not_nudged(db, expo, kw):
    stale_caregiver(db, **kw)
    assert nudge_stale_locations(db, NOON) == 0
    assert expo.messages == []


def test_a_caregiver_with_a_visit_en_route_or_in_progress_is_not_nudged(db, expo):
    patient = make_patient(db)
    for status in (BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS):
        cg = stale_caregiver(db)
        make_booking(db, patient, cg, status=status)
    assert nudge_stale_locations(db, NOON) == 0
    assert expo.messages == []


def test_an_accepted_visit_that_has_not_started_does_not_block_the_nudge(db, expo):
    cg = stale_caregiver(db)
    make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED)
    assert nudge_stale_locations(db, NOON) == 1


def test_at_most_one_nudge_per_gap(db, expo):
    cg = stale_caregiver(db)
    assert nudge_stale_locations(db, NOON) == 1
    gap = timedelta(hours=settings.LOCATION_NUDGE_MIN_GAP_HOURS)
    assert nudge_stale_locations(db, NOON + timedelta(minutes=15)) == 0
    assert nudge_stale_locations(db, NOON + gap - timedelta(minutes=1)) == 0
    assert len(expo.messages) == 1
    assert nudge_stale_locations(db, NOON + gap) == 1  # a day later, still stale: once more
    assert len(expo.messages) == 2
    assert location_of(db, cg).location_nudged_at == NOON + gap


def test_quiet_hours_boundaries_21_and_07_local():
    # Lagos is UTC+1 all year: 21:00 local = 20:00 UTC and 07:00 local = 06:00 UTC.
    assert in_quiet_hours(datetime(2026, 11, 25, 19, 59)) is False  # 20:59 local
    assert in_quiet_hours(datetime(2026, 11, 25, 20, 0)) is True  # 21:00 local
    assert in_quiet_hours(datetime(2026, 11, 26, 5, 59)) is True  # 06:59 local
    assert in_quiet_hours(datetime(2026, 11, 26, 6, 0)) is False  # 07:00 local


def test_no_nudge_during_quiet_hours_and_it_goes_out_after(db, expo):
    cg = stale_caregiver(db, now=datetime(2026, 11, 25, 22, 0))
    night = datetime(2026, 11, 25, 22, 0)  # 23:00 Lagos
    assert nudge_stale_locations(db, night) == 0
    assert expo.messages == [] and location_of(db, cg).location_nudged_at is None
    morning = datetime(2026, 11, 26, 6, 0)  # 07:00 Lagos
    assert nudge_stale_locations(db, morning) == 1


def test_a_failed_push_never_breaks_the_job_and_is_retried_next_run(db, expo):
    import httpx

    expo.raise_error = httpx.ConnectError("expo is down")
    cg = stale_caregiver(db)
    other = stale_caregiver(db)
    assert nudge_stale_locations(db, NOON) == 0  # no exception
    assert location_of(db, cg).location_nudged_at is None and location_of(db, other).location_nudged_at is None
    expo.raise_error = None
    assert nudge_stale_locations(db, NOON + timedelta(minutes=15)) == 2


def test_one_caregivers_failure_does_not_stop_the_others(db, expo):
    bad = stale_caregiver(db)
    good = stale_caregiver(db)
    bad_token = db.query(PushToken).filter_by(user_id=bad.user_id).one().token
    expo.other_errors[bad_token] = "MessageRateExceeded"
    assert nudge_stale_locations(db, NOON) == 1
    assert location_of(db, bad).location_nudged_at is None
    assert location_of(db, good).location_nudged_at == NOON
