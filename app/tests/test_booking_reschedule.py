"""Phase 2: a patient reschedules a paid booking (``app/services/booking_reschedule.py``, design in
``docs/plans/2026-11-25-booking-reschedule.md``).

Service calls take an explicit ``now`` so the 12 hour and 28 day boundaries are tested to the second; the HTTP tests use the
real clock. Races use real threads and sessions, like the cancel tests.
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.booking import (
    Booking, BookingDispatchWave, BookingNotification, BookingReschedule, BookingStatus, PaymentStatus,
)
from app.db.models.caregiver import CaregiverTransaction
from app.db.models.dropout import CaregiverDropout
from app.db.models.push_token import PushToken
from app.db.session import SessionLocal
from app.schemas.booking import BookingActionRequest
from app.services import booking_reschedule as resched, dispatch
from app.services.booking import BookingActionForbidden, BookingService
from app.services.patient_booking_cancel import PatientCancelError
from app.tests.booking_fixtures import make_prices
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.policy_helpers import set_policy
from app.tests.test_dispatch import make_candidate

API = "/api/v1/booking"
NOW = datetime(2026, 11, 25, 12, 0, 0)
TOTAL = 4_400_000


def hours(h, base=NOW):
    return base + timedelta(hours=h)


@pytest.fixture(autouse=True)
def _env(db):
    set_policy(db)        # the defaults: 12 hours, 2 free, 28 days
    make_prices(db)       # 4 hours of nurse = 4_400_000 kobo, what make_booking charges


@pytest.fixture
def patient(db):
    return make_patient(db)


def paid(db, patient, start_in=30, status=BookingStatus.PENDING, caregiver=None, **kw):
    cg = caregiver
    if status != BookingStatus.PENDING and cg is None:
        cg = make_caregiver(db)
    fields = dict(scheduled_date=hours(start_in), scheduled_end_date=hours(start_in + 4))
    fields.update(kw)
    return make_booking(db, patient, cg, status=status, visit_code="4821" if cg else None, **fields)


def go(db, patient, booking, new, now=NOW, **kw):
    return resched.reschedule(db, patient.user, booking.id, new, now=now, **kw)


def code_of(call):
    with pytest.raises(PatientCancelError) as err:
        call()
    return err.value.code


def reload(db, booking) -> Booking:
    db.expire_all()
    return db.get(Booking, booking.id)


def waves(db, booking):
    return db.query(BookingDispatchWave).filter_by(booking_id=booking.id).order_by(BookingDispatchWave.wave_no).all()


def history(db, booking):
    db.expire_all()
    return db.query(BookingReschedule).filter_by(booking_id=booking.id).order_by(BookingReschedule.created_at).all()


# --- the free window: more than 12 hours -------------------------------------------------------------------------------
@pytest.mark.parametrize("start_in_s, allowed", [
    (12 * 3600 + 1, True),     # one second over 12 h: offered
    (12 * 3600, False),        # exactly 12 h: not offered (the same line where the cancel fee starts)
    (3 * 3600, False),
])
def test_the_free_window_is_strictly_more_than_12_hours(db, patient, start_in_s, allowed):
    booking = paid(db, patient, start_in=0, scheduled_date=NOW + timedelta(seconds=start_in_s),
                   scheduled_end_date=NOW + timedelta(seconds=start_in_s, hours=4))
    new = hours(48)
    if allowed:
        assert go(db, patient, booking, new).booking.scheduled_date == new
    else:
        assert code_of(lambda: go(db, patient, booking, new)) == "reschedule_window_closed"
        assert reload(db, booking).scheduled_date == NOW + timedelta(seconds=start_in_s)
        assert reload(db, booking).reschedule_count == 0
        info = resched.list_info(reload(db, booking), resched.booking_policy.current(db), NOW)
        assert info["can_reschedule"] is False and info["reschedules_left"] == 2


def test_the_window_comes_from_the_admin_setting(db, patient):
    set_policy(db, reschedule_free_hours=24)
    booking = paid(db, patient, start_in=20)
    assert code_of(lambda: go(db, patient, booking, hours(100))) == "reschedule_window_closed"
    set_policy(db, reschedule_free_hours=6)
    assert go(db, patient, booking, hours(100)).booking.scheduled_date == hours(100)


# --- the new time: more than 12 hours from now, at most 28 days ----------------------------------------------------------
@pytest.mark.parametrize("delta, ok", [
    (timedelta(hours=12), False),                       # exactly the window: too soon
    (timedelta(hours=12, seconds=1), True),
    (timedelta(days=28), True),                         # exactly 28 days: allowed
    (timedelta(days=28, seconds=1), False),
])
def test_the_new_time_boundaries(db, patient, delta, ok):
    booking = paid(db, patient, start_in=100)
    new = NOW + delta
    if ok:
        assert go(db, patient, booking, new).booking.scheduled_date == new
    else:
        assert code_of(lambda: go(db, patient, booking, new)) == (
            "new_time_too_soon" if delta < timedelta(days=1) else "new_time_too_far")
        assert reload(db, booking).reschedule_count == 0 and not history(db, booking)


def test_the_same_time_is_refused_the_first_time(db, patient):
    booking = paid(db, patient)
    assert code_of(lambda: go(db, patient, booking, booking.scheduled_date)) == "same_time"


def test_the_range_comes_from_the_admin_setting(db, patient):
    set_policy(db, reschedule_max_days_ahead=7)
    booking = paid(db, patient)
    assert code_of(lambda: go(db, patient, booking, NOW + timedelta(days=7, seconds=1))) == "new_time_too_far"
    assert go(db, patient, booking, NOW + timedelta(days=7)).booking.scheduled_date == NOW + timedelta(days=7)


def test_a_new_time_with_an_offset_is_stored_as_utc(db, patient):
    booking = paid(db, patient)
    lagos = timezone(timedelta(hours=1))
    result = go(db, patient, booking, datetime(2026, 11, 28, 15, 0, tzinfo=lagos))
    assert result.booking.scheduled_date == datetime(2026, 11, 28, 14, 0)
    assert result.booking.scheduled_end_date == datetime(2026, 11, 28, 18, 0)   # start + the 240 booked minutes


# --- 2 free reschedules per booking ----------------------------------------------------------------------------------------
def test_two_free_reschedules_then_the_third_is_refused_with_a_support_message(db, patient):
    booking = paid(db, patient)
    first_count = go(db, patient, booking, hours(50)).booking.reschedule_count
    second = go(db, patient, booking, hours(60))
    assert (first_count, second.booking.reschedule_count) == (1, 2)
    assert second.as_dict()["reschedules_left"] == 0
    with pytest.raises(PatientCancelError) as err:
        go(db, patient, booking, hours(70))
    assert err.value.code == "reschedule_limit_reached" and "contact support" in err.value.message.lower()
    assert reload(db, booking).scheduled_date == hours(60) and len(history(db, booking)) == 2


def test_the_limit_comes_from_the_admin_setting(db, patient):
    set_policy(db, reschedule_max_free=1)
    booking = paid(db, patient)
    go(db, patient, booking, hours(50))
    assert code_of(lambda: go(db, patient, booking, hours(60))) == "reschedule_limit_reached"
    set_policy(db, reschedule_max_free=3)
    assert go(db, patient, booking, hours(60)).booking.reschedule_count == 2


# --- who and what state ------------------------------------------------------------------------------------------------------
def test_only_the_booker_may_reschedule(db, patient):
    booking = paid(db, patient)
    other = make_patient(db)
    assert code_of(lambda: go(db, other, booking, hours(50))) == "not_your_booking"
    with pytest.raises(PatientCancelError) as err:
        resched.reschedule(db, patient.user, uuid.uuid4(), hours(50), now=NOW)
    assert err.value.code == "booking_not_found"


@pytest.mark.parametrize("kw, code", [
    ({"payment_status": PaymentStatus.PENDING}, "booking_unpaid"),
    ({"payment_status": PaymentStatus.PARTIALLY_REFUNDED}, "booking_not_reschedulable"),
    ({"status": BookingStatus.CANCELLED}, "booking_already_cancelled"),
    ({"status": BookingStatus.EN_ROUTE, "on_my_way_at": hours(-1)}, "booking_not_reschedulable"),
    ({"status": BookingStatus.IN_PROGRESS}, "booking_not_reschedulable"),
    ({"status": BookingStatus.COMPLETED}, "booking_not_reschedulable"),
])
def test_only_a_paid_pending_or_accepted_booking_can_be_moved(db, patient, kw, code):
    booking = paid(db, patient, **kw)
    assert code_of(lambda: go(db, patient, booking, hours(50))) == code
    assert reload(db, booking).reschedule_count == 0


# --- the caregiver is released properly --------------------------------------------------------------------------------------
def test_an_assigned_caregiver_is_released_told_and_nothing_of_the_assignment_is_left(db, patient, expo):
    caregiver = make_candidate(db, at=NOW)
    db.add(PushToken(user_id=caregiver.user_id, token="ExponentPushToken[released]", platform="ios"))
    booking, _ = _offer_for(db, patient, caregiver)
    booking.on_my_way_at, booking.on_my_way_distance_m, booking.arrived_at = hours(28), 900, hours(29)
    booking.no_show_flagged_at = hours(31)
    db.commit()

    new = hours(60)
    result = go(db, patient, booking, new)

    b = reload(db, booking)
    assert (b.status, b.caregiver_id, b.visit_code_enc) == (BookingStatus.PENDING, None, None)
    assert (b.on_my_way_at, b.on_my_way_distance_m, b.arrived_at, b.no_show_flagged_at) == (None, None, None, None)
    assert (b.visit_code_failed_attempts, b.visit_code_locked_until) == (0, None)
    assert (b.scheduled_date, b.scheduled_end_date) == (new, new + timedelta(hours=4))
    assert result.as_dict()["caregiver_released"] is True
    # a notice for the released caregiver that is NOT a "new request" (it has a response), plus a push
    notice = db.query(BookingNotification).filter_by(booking_id=booking.id, caregiver_id=caregiver.id,
                                                      notification_type="booking_update").one()
    assert notice.response == "info" and "rescheduled" in notice.message.lower()
    assert any(m["to"] == "ExponentPushToken[released]" and m["data"] == {"type": "booking_rescheduled",
                                                                           "booking_id": str(booking.id)}
               for m in expo.messages)
    # not a drop-out, and no money or earning appeared or disappeared
    assert db.query(CaregiverDropout).filter_by(booking_id=booking.id).count() == 0
    assert db.query(CaregiverTransaction).filter_by(booking_id=booking.id).count() == 0
    assert (b.total_amount_kobo, b.payment_status) == (TOTAL, PaymentStatus.PAID)


def _offer_for(db, patient, caregiver, start_in=30):
    booking = paid(db, patient, start_in=start_in, status=BookingStatus.ACCEPTED, caregiver=caregiver)
    offer = BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="New Booking Request",
                                message="m", notification_type="new_booking", response="accept")
    db.add(offer)
    db.commit()
    return booking, offer


def test_stale_offers_are_closed_and_the_released_caregiver_is_offered_the_new_time_again(db, patient):
    released = make_candidate(db, 0, at=NOW)
    other = make_candidate(db, 1, at=NOW)
    booking, _ = _offer_for(db, patient, released)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=other.id, title="t", message="m",
                               notification_type="new_booking", response=None))   # an old wave's unanswered offer
    db.commit()

    go(db, patient, booking, hours(60))

    db.expire_all()
    wave = waves(db, booking)[-1]
    assert set(wave.notified_caregiver_ids) == {released.id, other.id}
    offers = {n.caregiver_id: n.response for n in db.query(BookingNotification).filter_by(
        booking_id=booking.id, notification_type="new_booking")}
    assert offers == {released.id: None, other.id: None}       # both re-offered: the new wave reset them
    assert reload(db, booking).dispatch_state == "dispatching"


def test_a_caregiver_who_is_not_re_offered_cannot_accept_the_stale_offer(db, patient):
    stale = make_caregiver(db)                                      # no location: never a candidate
    booking = paid(db, patient, start_in=30)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=stale.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    go(db, patient, booking, hours(60))
    with pytest.raises(BookingActionForbidden) as err:
        BookingService(db).handle_booking_action(booking.id, stale.id, BookingActionRequest(action="accept"))
    assert "rescheduled" in str(err.value).lower()
    assert reload(db, booking).status == BookingStatus.PENDING


def test_an_auto_declined_offer_and_an_info_notice_cannot_be_used_to_accept_the_stale_time(db, patient):
    """B was offered with A, A accepted (B's row became auto_declined), then the visit was moved and B was not re-offered."""
    a = make_candidate(db, 0, at=NOW)
    b = make_caregiver(db)                                           # no location: never re-offered
    booking, _ = _offer_for(db, patient, a)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=b.id, title="t", message="m",
                               notification_type="new_booking", response="auto_declined"))
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=b.id, title="Visit reassigned", message="m",
                               notification_type="booking_update", response="info"))   # a notice row for the same person
    db.commit()
    go(db, patient, booking, hours(60))
    with pytest.raises(BookingActionForbidden) as err:
        BookingService(db).handle_booking_action(booking.id, b.id, BookingActionRequest(action="accept"))
    assert "rescheduled" in str(err.value).lower()
    assert reload(db, booking).status == BookingStatus.PENDING


def test_a_re_offered_caregiver_can_accept_the_new_time(db, patient):
    cg = make_candidate(db, at=datetime.utcnow())
    booking = paid(db, patient, start_in=30, scheduled_date=datetime.utcnow() + timedelta(hours=30),
                   scheduled_end_date=datetime.utcnow() + timedelta(hours=34))
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=cg.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    go(db, patient, booking, datetime.utcnow() + timedelta(hours=60), now=datetime.utcnow())
    accepted = BookingService(db).handle_booking_action(booking.id, cg.id, BookingActionRequest(action="accept"))
    assert accepted.status == BookingStatus.ACCEPTED and accepted.caregiver_id == cg.id


# --- an unassigned booking, and re-matching -----------------------------------------------------------------------------------
def test_a_booking_still_looking_for_a_caregiver_is_moved_and_dispatched_again(db, patient):
    cg = make_candidate(db, at=NOW)
    booking = paid(db, patient, dispatch_state="unmatched", dispatch_started_at=hours(-1))
    result = go(db, patient, booking, hours(60))
    b = reload(db, booking)
    assert (b.status, b.caregiver_id, b.dispatch_state, b.dispatch_started_at) == (
        BookingStatus.PENDING, None, "dispatching", NOW)
    assert [w.notified_caregiver_ids for w in waves(db, booking)] == [[cg.id]]
    assert result.as_dict()["caregiver_released"] is False and result.booking.reschedule_count == 1


def test_nobody_accepting_ends_in_the_admin_unmatched_queue(db, patient):
    booking = paid(db, patient, start_in=30)            # no caregiver is online at all
    go(db, patient, booking, hours(60))
    assert reload(db, booking).dispatch_state == "dispatching"
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=5))
    assert reload(db, booking).dispatch_state == "dispatching"           # still trying
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=31))              # DISPATCH_ESCALATE_MINUTES: the existing fallback
    b = reload(db, booking)
    assert (b.dispatch_state, b.status, b.caregiver_id) == ("unmatched", BookingStatus.PENDING, None)


def test_a_booking_held_for_an_address_check_stays_held(db, patient):
    make_candidate(db, at=NOW)
    booking = paid(db, patient, needs_address_check=True)
    go(db, patient, booking, hours(60))
    b = reload(db, booking)
    assert b.dispatch_state is None and waves(db, booking) == [] and b.needs_address_check is True


def test_a_failing_first_wave_does_not_undo_the_reschedule_and_the_tick_recovers(db, patient, monkeypatch):
    cg = make_candidate(db, at=NOW)
    booking = paid(db, patient)

    def boom(*a, **k):
        raise RuntimeError("candidate query failed")

    monkeypatch.setattr(dispatch, "send_wave", boom)
    go(db, patient, booking, hours(60))
    b = reload(db, booking)
    assert b.scheduled_date == hours(60) and b.dispatch_state == "dispatching" and waves(db, booking) == []
    monkeypatch.undo()
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=1))
    assert [w.notified_caregiver_ids for w in waves(db, booking)] == [[cg.id]]


def test_an_open_dropout_journey_is_closed_by_the_reschedule(db, patient):
    booking = paid(db, patient, dropout_state="awaiting_choice", channel="app")
    go(db, patient, booking, hours(60))
    assert reload(db, booking).dropout_state is None


# --- the price ---------------------------------------------------------------------------------------------------------------------
def test_the_same_price_changes_nothing_about_money(db, patient):
    booking = paid(db, patient)
    result = go(db, patient, booking, hours(60))
    price = result.as_dict()["price"]
    assert (price["paid_amount_kobo"], price["new_amount_kobo"], price["difference_kobo"], price["handling"]) == (
        TOTAL, TOTAL, 0, "none")
    row = history(db, booking)[0]
    assert (row.price_difference_kobo, row.price_handling) == (0, "none")


def test_a_lower_price_is_recorded_as_a_credit_and_the_booking_keeps_what_was_paid(db, patient):
    booking = paid(db, patient, total_amount_kobo=5_000_000)       # paid 50,000; the price now is 44,000
    result = go(db, patient, booking, hours(60))
    b = reload(db, booking)
    assert (b.total_amount_kobo, b.service_fee_kobo, b.payment_status) == (5_000_000, round(5_000_000 / 1.1), PaymentStatus.PAID)
    price = result.as_dict()["price"]
    assert (price["difference_kobo"], price["credit_kobo"], price["handling"]) == (-600_000, 600_000, "credit_due")
    row = history(db, booking)[0]
    assert (row.old_total_kobo, row.new_total_kobo, row.price_difference_kobo, row.price_handling) == (
        5_000_000, TOTAL, -600_000, "credit_due")
    assert b.refund_required is False        # nothing is queued: a partial refund now would break the paid state


def test_a_higher_price_is_refused_and_nothing_changes(db, patient):
    booking = paid(db, patient, total_amount_kobo=3_300_000)       # paid 33,000; the price now is 44,000
    cg = make_candidate(db, at=NOW)
    with pytest.raises(PatientCancelError) as err:
        go(db, patient, booking, hours(60))
    assert err.value.status_code == 409 and err.value.code == "price_increase_needs_support"
    assert err.value.extra["price"]["difference_kobo"] == 1_100_000
    b = reload(db, booking)
    assert (b.scheduled_date, b.reschedule_count, b.status, b.dispatch_state) == (hours(30), 0, BookingStatus.PENDING, None)
    assert not history(db, booking) and not waves(db, booking)
    assert cg is not None


def test_surge_switched_on_after_the_booking_makes_the_visit_dearer_and_refuses_it(db, patient):
    make_prices(db, surge_pricing=True, surge_multiplier=1.5)      # the newest pricing version wins
    booking = paid(db, patient)
    assert code_of(lambda: go(db, patient, booking, hours(60))) == "price_increase_needs_support"


def test_a_price_that_changed_since_the_person_looked_is_caught(db, patient):
    booking = paid(db, patient, total_amount_kobo=5_000_000)
    with pytest.raises(PatientCancelError) as err:
        go(db, patient, booking, hours(60), accepted_total_kobo=5_000_000)
    assert err.value.code == "price_changed" and err.value.extra["price"]["new_amount_kobo"] == TOTAL
    assert reload(db, booking).reschedule_count == 0
    assert go(db, patient, booking, hours(60), accepted_total_kobo=TOTAL).booking.reschedule_count == 1


def test_a_service_that_is_no_longer_offered_blocks_the_move(db, patient):
    from app.db.models.service_catalog import ServiceCatalog
    db.query(ServiceCatalog).filter(ServiceCatalog.service_type == "nurse").update({"enabled": False})
    db.commit()
    booking = paid(db, patient)
    assert code_of(lambda: go(db, patient, booking, hours(60))) == "service_unavailable"


def test_no_pricing_row_is_a_503_and_nothing_changes(db, patient):
    from app.db.models.booking_settings import BookingSettings
    db.query(BookingSettings).delete()
    db.commit()
    booking = paid(db, patient)
    with pytest.raises(PatientCancelError) as err:
        go(db, patient, booking, hours(60))
    assert err.value.status_code == 503 and err.value.code == "booking_settings_not_configured"
    assert reload(db, booking).reschedule_count == 0


# --- idempotency ---------------------------------------------------------------------------------------------------------------
def test_the_same_request_id_twice_is_one_reschedule(db, patient):
    booking = paid(db, patient)
    first = go(db, patient, booking, hours(60), request_id="req-1")
    again = go(db, patient, booking, hours(60), request_id="req-1")
    assert (first.already, again.already) == (False, True)
    assert again.booking.reschedule_count == 1 and len(history(db, booking)) == 1 and len(waves(db, booking)) <= 1
    assert again.as_dict()["scheduled_date"] == hours(60)


def test_a_replay_is_answered_even_when_the_rules_would_now_refuse_it(db, patient):
    set_policy(db, reschedule_max_free=1)
    booking = paid(db, patient)
    go(db, patient, booking, hours(60), request_id="req-1")           # the limit is now used up
    assert go(db, patient, booking, hours(60), request_id="req-1").already is True


def test_a_request_id_cannot_be_reused_for_another_time(db, patient):
    booking = paid(db, patient)
    go(db, patient, booking, hours(60), request_id="req-1")
    assert code_of(lambda: go(db, patient, booking, hours(70), request_id="req-1")) == "request_id_reused"


def test_repeating_the_same_target_without_a_request_id_replays_instead_of_erroring(db, patient):
    booking = paid(db, patient)
    go(db, patient, booking, hours(60))
    again = go(db, patient, booking, hours(60))
    assert again.already is True and reload(db, booking).reschedule_count == 1


def test_a_different_time_with_a_new_request_id_counts_as_a_new_reschedule(db, patient):
    booking = paid(db, patient)
    go(db, patient, booking, hours(60), request_id="a")
    assert go(db, patient, booking, hours(70), request_id="b").booking.reschedule_count == 2


def test_an_over_long_request_id_is_refused(db, patient):
    booking = paid(db, patient)
    assert code_of(lambda: go(db, patient, booking, hours(60), request_id="x" * 65)) == "request_id_too_long"


# --- races -----------------------------------------------------------------------------------------------------------------------
def race(*jobs):
    barrier, results = threading.Barrier(len(jobs)), {}

    def run(name, job):
        s = SessionLocal()
        try:
            barrier.wait()
            try:
                results[name] = job(s)
            except Exception as exc:  # noqa: BLE001 - the outcome is what the test inspects
                results[name] = exc
        finally:
            s.close()

    threads = [threading.Thread(target=run, args=job) for job in jobs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def do_reschedule(user, booking, new, **kw):
    return lambda s: resched.reschedule(s, s.merge(user, load=False), booking.id, new, now=NOW, **kw)


def test_a_double_tap_from_two_devices_is_one_reschedule_one_wave(db, patient):
    make_candidate(db, at=NOW)
    booking = paid(db, patient)
    new = hours(60)
    out = race(("a", do_reschedule(patient.user, booking, new, request_id="same")),
               ("b", do_reschedule(patient.user, booking, new, request_id="same")),
               ("c", do_reschedule(patient.user, booking, new)))
    assert all(isinstance(r, resched.Result) for r in out.values()), out
    assert sorted(r.already for r in out.values()) == [False, True, True]
    assert reload(db, booking).reschedule_count == 1 and len(history(db, booking)) == 1 and len(waves(db, booking)) == 1


def test_three_different_simultaneous_reschedules_never_exceed_the_limit(db, patient):
    booking = paid(db, patient)
    out = race(("a", do_reschedule(patient.user, booking, hours(50))),
               ("b", do_reschedule(patient.user, booking, hours(60))),
               ("c", do_reschedule(patient.user, booking, hours(70))))
    done = [r for r in out.values() if isinstance(r, resched.Result) and not r.already]
    refused = [r for r in out.values() if isinstance(r, PatientCancelError)]
    assert len(done) == 2 and [e.code for e in refused] == ["reschedule_limit_reached"], out
    assert reload(db, booking).reschedule_count == 2 and len(history(db, booking)) == 2


def test_reschedule_racing_a_caregiver_accept_never_leaves_a_stale_assignment(db, patient):
    caregiver = make_caregiver(db)                                  # not a candidate: a stale offer is never re-offered
    booking = paid(db, patient, start_in=30)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    new = hours(60)

    def accept(s):
        return BookingService(s).handle_booking_action(booking.id, caregiver.id, BookingActionRequest(action="accept"))

    out = race(("accept", accept), ("move", do_reschedule(patient.user, booking, new)))
    assert isinstance(out["move"], resched.Result), out
    b = reload(db, booking)
    # whichever won, the visit is at the new time, open for matching, and nobody holds the old assignment
    assert (b.scheduled_date, b.status, b.caregiver_id, b.visit_code_enc, b.reschedule_count) == (
        new, BookingStatus.PENDING, None, None, 1)
    if isinstance(out["accept"], BookingActionForbidden):           # the reschedule came first: the stale offer was refused
        assert db.query(BookingNotification).filter_by(booking_id=booking.id, notification_type="booking_update").count() == 0
    else:                                                            # the accept came first: that caregiver was released and told
        assert out["accept"].status == BookingStatus.ACCEPTED
        assert db.query(BookingNotification).filter_by(booking_id=booking.id, caregiver_id=caregiver.id,
                                                        notification_type="booking_update", response="info").count() == 1


def test_a_cancel_after_a_reschedule_is_free_because_the_new_time_is_more_than_12_hours_out(db, patient):
    from app.services import patient_booking_cancel
    booking = paid(db, patient, start_in=14, status=BookingStatus.ACCEPTED)    # 14 h out: inside the fee window? no: > 12 h
    go(db, patient, booking, hours(40))
    result = patient_booking_cancel.cancel(db, patient.user, booking.id, now=NOW)
    assert (result.view.fee_tier, result.view.fee_kobo) == ("none", 0)


# --- HTTP ---------------------------------------------------------------------------------------------------------------------------
def real_booking(db, patient, start_in=30, **kw):
    start = datetime.utcnow() + timedelta(hours=start_in)
    return make_booking(db, patient, scheduled_date=start, scheduled_end_date=start + timedelta(hours=4), **kw)


def iso(dt):
    return dt.isoformat() + "Z"


def test_terms_then_reschedule_over_http(db, client, patient):
    booking = real_booking(db, patient)
    h = auth_headers(patient.user)
    new = datetime.utcnow() + timedelta(hours=72)

    terms = client.get(f"{API}/{booking.id}/reschedule-terms", params={"new_scheduled_date": iso(new)}, headers=h)
    body = terms.json()
    assert terms.status_code == 200 and body["can_reschedule"] is True and body["new_time_ok"] is True
    assert (body["free_hours"], body["reschedules_left"], body["reschedules_allowed"]) == (12, 2, 2)
    assert body["price"]["new_amount_kobo"] == TOTAL and body["price"]["handling"] == "none"

    response = client.post(f"{API}/{booking.id}/reschedule", headers=h, json={
        "new_scheduled_date": iso(new), "request_id": "req-http-1", "accepted_total_kobo": TOTAL})
    out = response.json()
    assert response.status_code == 200, out
    assert (out["already_rescheduled"], out["reschedules_used"], out["reschedules_left"], out["status"]) == (
        False, 1, 1, "pending")
    assert out["price"]["difference_kobo"] == 0 and out["caregiver_released"] is False
    again = client.post(f"{API}/{booking.id}/reschedule", headers=h, json={"new_scheduled_date": iso(new), "request_id": "req-http-1"})
    assert again.status_code == 200 and again.json()["already_rescheduled"] is True and again.json()["reschedules_used"] == 1


def test_error_shapes_over_http(db, client, patient):
    booking = real_booking(db, patient, start_in=3)
    h = auth_headers(patient.user)
    terms = client.get(f"{API}/{booking.id}/reschedule-terms", headers=h).json()
    assert terms["can_reschedule"] is False and terms["reason"] == "reschedule_window_closed" and terms["price"] is None
    r = client.post(f"{API}/{booking.id}/reschedule", headers=h,
                    json={"new_scheduled_date": iso(datetime.utcnow() + timedelta(hours=72))})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "reschedule_window_closed"
    assert set(r.json()["detail"]) == {"code", "message"}
    far = real_booking(db, patient)
    r = client.post(f"{API}/{far.id}/reschedule", headers=h,
                    json={"new_scheduled_date": iso(datetime.utcnow() + timedelta(days=29))})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "new_time_too_far"
    assert client.post(f"{API}/{far.id}/reschedule", headers=h, json={}).status_code == 422
    stranger = auth_headers(make_patient(db).user)
    assert client.get(f"{API}/{far.id}/reschedule-terms", headers=stranger).status_code == 403


def test_the_booking_list_tells_the_app_whether_to_show_the_option(db, client, patient):
    far, near = real_booking(db, patient, start_in=40), real_booking(db, patient, start_in=5)
    rows = {b["id"]: b for b in client.get(f"{API}/patient/bookings", headers=auth_headers(patient.user)).json()}
    assert (rows[str(far.id)]["can_reschedule"], rows[str(far.id)]["reschedules_left"], rows[str(far.id)]["reschedule_count"]) == (True, 2, 0)
    assert (rows[str(near.id)]["can_reschedule"], rows[str(near.id)]["reschedules_left"]) == (False, 2)
    single = client.get(f"{API}/booking/{far.id}", headers=auth_headers(patient.user)).json()
    assert single["can_reschedule"] is True


def test_a_caregiver_or_admin_view_never_carries_the_reschedule_fields(db, client, patient):
    caregiver = make_caregiver(db)
    booking = real_booking(db, patient, status=BookingStatus.ACCEPTED, caregiver_id=caregiver.id)
    body = client.get(f"{API}/booking/{booking.id}", headers=auth_headers(caregiver.user)).json()
    assert (body["can_reschedule"], body["reschedule_count"], body["reschedules_left"]) == (None, None, None)


# --- the admin settings ----------------------------------------------------------------------------------------------------------
def test_admin_can_edit_the_three_settings_and_bad_values_are_refused(db, client):
    h = auth_headers(make_admin(db, ["super_admin"], password=None))
    url = "/api/v1/admin/booking-policy"
    assert client.get(url, headers=h).json()["reschedule_max_days_ahead"] == 28
    ok = client.patch(url, headers=h, json={"reason": "Pilot", "reschedule_free_hours": 24, "reschedule_max_free": 3,
                                            "reschedule_max_days_ahead": 14})
    assert ok.status_code == 200 and (ok.json()["reschedule_free_hours"], ok.json()["reschedule_max_free"],
                                      ok.json()["reschedule_max_days_ahead"]) == (24, 3, 14)
    for bad in ({"reschedule_max_days_ahead": 0}, {"reschedule_max_days_ahead": 366}, {"reschedule_max_free": 21},
                {"reschedule_free_hours": 169}, {"reschedule_max_free": -1}):
        assert client.patch(url, headers=h, json={"reason": "x", **bad}).status_code == 422, bad
    assert client.get(url, headers=h).json()["reschedule_max_days_ahead"] == 14


def test_admin_booking_detail_shows_the_reschedule_history_and_what_is_owed(db, client, patient):
    booking = paid(db, patient, total_amount_kobo=5_000_000)
    go(db, patient, booking, hours(60), request_id="r1")
    h = auth_headers(make_admin(db, ["super_admin"], password=None))
    body = client.get(f"/api/v1/admin/bookings/{booking.id}", headers=h).json()
    assert body["reschedule_count"] == 1 and len(body["reschedules"]) == 1
    row = body["reschedules"][0]
    assert (row["old_total_kobo"], row["new_total_kobo"], row["price_difference_kobo"], row["price_handling"],
            row["caregiver_released"]) == (5_000_000, TOTAL, -600_000, "credit_due", False)
    assert row["new_scheduled_date"] == hours(60).isoformat()
