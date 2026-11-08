"""The booking's own cancel terms on the admin booking detail: the stored snapshot, the tiers in force, what a booker
cancel would cost right now (same function as the patient's quote) and what a past cancel applied."""

from datetime import datetime, timedelta

import pytest

from app.db.models.booking import BookingStatus, PaymentStatus
from app.services import booking_policy, patient_booking_cancel
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.policy_helpers import set_policy

BASE = "/api/v1/admin/bookings"
SNAPSHOT = {"free_hours": 24, "late_hours": 6, "fee_percent": 20, "late_fee_percent": 50}
TIERS_KEYS = {"free_hours", "late_hours", "fee_percent", "late_fee_percent"}


@pytest.fixture
def ops(db):
    return auth_headers(make_admin(db, ["operations"], password=None))


def detail(client, headers, booking):
    r = client.get(f"{BASE}/{booking.id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["cancel_terms"]


def accepted(db, hours_ahead, **kw):
    start = datetime.utcnow() + timedelta(hours=hours_ahead)
    kw.setdefault("status", BookingStatus.ACCEPTED)
    kw.setdefault("cancel_policy", SNAPSHOT)
    return make_booking(db, make_patient(db), make_caregiver(db), scheduled_date=start,
                        scheduled_end_date=start + timedelta(hours=4), **kw)


def test_the_snapshot_and_the_tier_that_applies_now(client, db, ops):
    for hours, tier, percent in ((48, "none", 0), (12, "standard", 20), (2, "late", 50)):
        b = accepted(db, hours)
        t = detail(client, ops, b)
        assert t["policy_source"] == "booking_snapshot"
        assert t["snapshot"] == SNAPSHOT and t["policy"] == SNAPSHOT
        now = t["would_apply_now"]
        assert now["allowed"] is True and now["refusal"] is None and now["tier"] == tier and now["fee_percent"] == percent
        assert now["fee_kobo"] == (int(b.service_fee_kobo) * percent + 50) // 100
        assert now["refund_kobo"] == int(b.total_amount_kobo) - now["fee_kobo"]
        assert t["applied"] is None


def test_the_booking_keeps_its_own_terms_when_the_live_policy_changes(client, db, ops):
    b = accepted(db, 12)
    set_policy(db, cancel_free_hours=2, cancel_late_hours=1, cancel_fee_percent=90, cancel_late_fee_percent=100)
    t = detail(client, ops, b)
    assert t["snapshot"] == SNAPSHOT and t["would_apply_now"]["tier"] == "standard"
    assert t["would_apply_now"]["fee_percent"] == 20


def test_an_older_booking_without_a_snapshot_uses_the_live_policy(client, db, ops):
    b = accepted(db, 12, cancel_policy=None)
    live = booking_policy.cancel_snapshot(booking_policy.current(db))
    t = detail(client, ops, b)
    assert t["policy_source"] == "live_policy" and t["snapshot"] is None and t["policy"] == live


@pytest.mark.parametrize("status,payment,refusal", [
    (BookingStatus.IN_PROGRESS, PaymentStatus.PAID, "in_progress"),
    (BookingStatus.COMPLETED, PaymentStatus.PAID, "closed"),
    (BookingStatus.PENDING, PaymentStatus.PENDING, "unpaid"),
])
def test_a_booking_that_cannot_be_cancelled_says_why(client, db, ops, status, payment, refusal):
    b = make_booking(db, make_patient(db), make_caregiver(db), status=status, payment_status=payment, cancel_policy=SNAPSHOT)
    now = detail(client, ops, b)["would_apply_now"]
    assert now["allowed"] is False and now["refusal"] == refusal and now["tier"] is None
    assert now["fee_kobo"] == 0 and now["refund_kobo"] == 0


def test_a_booker_cancel_shows_the_applied_tier_and_fee(client, db, ops):
    b = accepted(db, 2, cancel_fee_tier="late", cancel_reason="cancelled_by_booker", status=BookingStatus.CANCELLED,
                 late_cancel_compensable=False)
    t = detail(client, ops, b)
    assert t["applied"] == {"tier": "late", "fee_kobo": patient_booking_cancel.cancel_fee_kobo(b),
                            "late_cancel_compensable": False}
    assert t["applied"]["fee_kobo"] == (int(b.service_fee_kobo) * 50 + 50) // 100
    assert set(t["snapshot"]) == TIERS_KEYS
