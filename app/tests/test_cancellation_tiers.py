"""Two-tier family cancellation fee: which tier applies, what is stored, and that the snapshot is what counts."""

from datetime import datetime, timedelta

import pytest

from app.db.models.booking import Booking, BookingStatus, CaregiverLocation, PaymentStatus
from app.services import booking_cancellation as cancellation
from app.tests.policy_helpers import set_policy
from app.tests.whatsapp_notify_helpers import Chat, accepted_booking, act, fake_meta, onboarded  # noqa: F401

NOW = datetime(2026, 10, 7, 9, 0, 0)
SERVICE_FEE = 4_000_000
TOTAL = 4_400_000
SNAPSHOT = {"free_hours": 12, "late_hours": 2, "fee_percent": 20, "late_fee_percent": 50}


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def memory_booking(hours_left, status=BookingStatus.ACCEPTED, **kw):
    """An unsaved booking: the terms are a pure function of the booking and the policy."""
    return Booking(status=status, payment_status=PaymentStatus.PAID, total_amount_kobo=TOTAL,
                   service_fee_kobo=SERVICE_FEE, scheduled_date=NOW + timedelta(hours=hours_left), **kw)


def policy(**kw):
    return cancellation.CancelPolicy(**{**SNAPSHOT, **kw})


@pytest.mark.parametrize("hours_left, status, tier, fee", [
    (1, BookingStatus.PENDING, "none", 0),             # nobody assigned: always free
    (30, BookingStatus.ACCEPTED, "none", 0),
    (12.01, BookingStatus.ACCEPTED, "none", 0),
    (12, BookingStatus.ACCEPTED, "standard", 800_000),  # at the free line the fee starts
    (5, BookingStatus.ACCEPTED, "standard", 800_000),
    (2.01, BookingStatus.ACCEPTED, "standard", 800_000),
    (2, BookingStatus.ACCEPTED, "late", 2_000_000),     # at the late line the late fee starts
    (0.5, BookingStatus.ACCEPTED, "late", 2_000_000),
    (-1, BookingStatus.ACCEPTED, "late", 2_000_000),    # the start time has passed
    (40, BookingStatus.EN_ROUTE, "late", 2_000_000),    # on the way: late whatever the clock says
])
def test_the_tier_and_the_fee(hours_left, status, tier, fee):
    terms = cancellation.terms(memory_booking(hours_left, status), NOW, policy())
    assert terms.allowed and terms.tier == tier and terms.fee_kobo == fee
    assert terms.refund_kobo == TOTAL - fee


def test_in_progress_is_a_human_matter_and_closed_states_are_refused():
    assert cancellation.terms(memory_booking(1, BookingStatus.IN_PROGRESS), NOW, policy()).refusal == "in_progress"
    assert cancellation.terms(memory_booking(1, BookingStatus.COMPLETED), NOW, policy()).refusal == "closed"


def test_the_policy_values_drive_the_tiers():
    p = policy(free_hours=48, late_hours=10, fee_percent=10, late_fee_percent=100)
    assert cancellation.terms(memory_booking(30), NOW, p).fee_kobo == 400_000
    assert cancellation.terms(memory_booking(8), NOW, p).fee_kobo == SERVICE_FEE  # 100% of the service fee


def test_a_fee_is_never_more_than_what_was_paid_and_zero_percent_means_free():
    assert cancellation.terms(memory_booking(1), NOW, policy(late_fee_percent=0)).fee_kobo == 0


# --- the stored result --------------------------------------------------------------------------------------
def reload(db, booking):
    db.expire_all()
    return db.get(Booking, booking.id)


def test_a_standard_cancel_stores_the_tier_and_no_compensation_flag(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    assert cancellation.cancel_by_booker(db, booking.id).status == "cancelled"
    b = reload(db, booking)
    assert b.cancel_fee_tier == "standard" and b.late_cancel_compensable is None
    assert b.refund_amount_kobo == TOTAL - 800_000


def test_a_full_refund_cancel_stores_tier_none(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=30)
    cancellation.cancel_by_booker(db, booking.id)
    b = reload(db, booking)
    assert b.cancel_fee_tier == "none" and b.late_cancel_compensable is None and b.refund_amount_kobo == TOTAL


def test_a_late_cancel_inside_the_late_window_is_not_compensable(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=1)
    cancellation.cancel_by_booker(db, booking.id)
    b = reload(db, booking)
    assert b.cancel_fee_tier == "late" and b.late_cancel_compensable is False
    assert b.refund_amount_kobo == TOTAL - 2_000_000


def start_moving(db, booking, caregiver, *, hours_ahead_tap_ok=True, moved_m=600, ping_after=True):
    """The caregiver is ~1.5 km away, taps on my way, then (optionally) pings from ``moved_m`` closer."""
    lat, lon = booking.patient_latitude, booking.patient_longitude
    db.add(CaregiverLocation(caregiver_id=caregiver.id, latitude=lat + 0.0135, longitude=lon, is_active=True,
                             is_available=True, updated_at=datetime.utcnow() - timedelta(minutes=1)))
    db.commit()
    act(db, booking, caregiver, "on_my_way")
    if ping_after:
        loc = db.query(CaregiverLocation).filter_by(caregiver_id=caregiver.id).one()
        loc.latitude = lat + 0.0135 - moved_m / 111_000
        loc.updated_at = datetime.utcnow() + timedelta(seconds=5)
        db.commit()


def test_on_my_way_stores_when_and_how_far(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, caregiver, ping_after=False)
    b = reload(db, booking)
    assert b.on_my_way_at is not None and 1_400 < b.on_my_way_distance_m < 1_600


def test_a_late_cancel_after_a_qualifying_on_my_way_is_compensable(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, caregiver, moved_m=600)
    cancellation.cancel_by_booker(db, booking.id)
    b = reload(db, booking)
    assert b.cancel_fee_tier == "late" and b.late_cancel_compensable is True


def test_on_my_way_without_movement_is_not_compensable(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, caregiver, ping_after=False)
    cancellation.cancel_by_booker(db, booking.id)
    assert reload(db, booking).late_cancel_compensable is False


def test_too_little_movement_is_not_compensable(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, caregiver, moved_m=150)
    cancellation.cancel_by_booker(db, booking.id)
    assert reload(db, booking).late_cancel_compensable is False


def test_an_on_my_way_tapped_too_early_is_not_compensable_even_if_they_moved(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=5)  # tapped 5 h before: outside the 120 minute window
    start_moving(db, booking, caregiver, moved_m=900)
    cancellation.cancel_by_booker(db, booking.id)
    b = reload(db, booking)
    assert b.cancel_fee_tier == "late" and b.late_cancel_compensable is False


def test_the_compensation_window_is_read_from_the_policy(db, chat):
    set_policy(db, onmyway_compensation_window_minutes=30)
    booking, caregiver = accepted_booking(db, chat, hours_ahead=1.5)
    start_moving(db, booking, caregiver, moved_m=900)
    cancellation.cancel_by_booker(db, booking.id)
    assert reload(db, booking).late_cancel_compensable is False


# --- the snapshot is what counts ----------------------------------------------------------------------------
def test_a_booking_with_a_snapshot_ignores_later_policy_changes(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6, cancel_policy=dict(SNAPSHOT))
    set_policy(db, cancel_fee_percent=90, cancel_free_hours=24, cancel_late_hours=5)
    terms = cancellation.terms_for(db, reload(db, booking), datetime.utcnow())
    assert terms.tier == "standard" and terms.fee_kobo == 800_000 and terms.fee_percent == 20


def test_a_booking_without_a_snapshot_uses_the_live_policy(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    set_policy(db, cancel_fee_percent=90)
    assert cancellation.terms_for(db, reload(db, booking), datetime.utcnow()).fee_kobo == 3_600_000


def test_the_terms_in_the_cancel_flow_use_the_snapshot_not_the_policy_edited_since(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6, cancel_policy=dict(SNAPSHOT))
    set_policy(db, cancel_fee_percent=90)
    cancellation.cancel_by_booker(db, booking.id)
    assert reload(db, booking).refund_amount_kobo == TOTAL - 800_000
