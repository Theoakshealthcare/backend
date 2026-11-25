"""Follow-ups from the Opus review of #87: the credit is the LATEST quote only and is queued for Finance at completion,
the dispatch run restarts at a reschedule (old waves neither block nor shorten the new matching), and a decline stays final.
"""

from datetime import timedelta

import pytest

from app.core.config import settings
from app.db.models.booking import BookingDispatchWave, BookingNotification, BookingStatus
from app.db.models.refund import Refund
from app.schemas.booking import BookingActionRequest
from app.services import booking_reschedule as resched, dispatch
from app.services.booking import BookingService
from app.tests.booking_fixtures import make_prices
from app.tests.factories import auth_headers, make_admin, make_caregiver
from app.tests.test_booking_reschedule import (  # noqa: F401  (fixtures and helpers shared with the main file)
    NOW, _env, go, history, hours, patient, paid, reload, waves,
)
from app.tests.test_dispatch import make_candidate

PAID = 1_000_000


def prices(db, per_hour_kobo):
    """A pricing version where 4 hours of nurse cost exactly ``4 * per_hour_kobo`` (no platform fee)."""
    make_prices(db, base_fee_kobo=per_hour_kobo, platform_fee_percent=0.0)


def at(minutes):
    return NOW + timedelta(minutes=minutes)


# --- 1. the amount owed is the latest quote only --------------------------------------------------------------------------
def test_a_credit_that_was_undone_by_a_later_quote_is_not_owed(db, patient):
    booking = paid(db, patient, total_amount_kobo=PAID)
    prices(db, 200_000)                                  # quote 800,000: credit 200,000
    go(db, patient, booking, hours(60), now=at(0))
    assert resched.credit_owed_kobo(db, booking.id) == 200_000
    prices(db, 250_000)                                  # quote 1,000,000 = what was paid: nothing owed
    go(db, patient, booking, hours(70), now=at(1))
    rows = history(db, booking)
    assert [r.price_difference_kobo for r in rows] == [-200_000, 0]
    assert resched.credit_owed_kobo(db, booking.id) == 0


def test_two_lower_quotes_are_not_added_together(db, patient):
    booking = paid(db, patient, total_amount_kobo=PAID)
    prices(db, 200_000)                                  # 800,000: credit 200,000
    go(db, patient, booking, hours(60), now=at(0))
    prices(db, 225_000)                                  # 900,000: credit 100,000 (NOT 200,000 + 100,000)
    go(db, patient, booking, hours(70), now=at(1))
    assert resched.credit_owed_kobo(db, booking.id) == 100_000


def test_the_admin_detail_marks_older_rows_superseded_and_shows_the_one_amount_owed(db, client, patient):
    booking = paid(db, patient, total_amount_kobo=PAID)
    prices(db, 200_000)
    go(db, patient, booking, hours(60), now=at(0))
    prices(db, 225_000)
    go(db, patient, booking, hours(70), now=at(1))
    h = auth_headers(make_admin(db, ["super_admin"], password=None))
    body = client.get(f"/api/v1/admin/bookings/{booking.id}", headers=h).json()
    assert body["reschedule_credit_due_kobo"] == 100_000
    assert [(r["price_difference_kobo"], r["superseded"]) for r in body["reschedules"]] == [(-200_000, True), (-100_000, False)]


def _in_progress_with_credit(db, patient, per_hour=200_000, extra=None):
    booking = paid(db, patient, total_amount_kobo=PAID)
    prices(db, per_hour)
    go(db, patient, booking, hours(60), now=at(0))
    cg = make_caregiver(db)
    booking = reload(db, booking)
    booking.caregiver_id, booking.status, booking.actual_start_time = cg.id, BookingStatus.IN_PROGRESS, hours(59)
    booking.dispatch_state = "matched"
    db.commit()
    return booking, cg


def _complete(db, booking, cg):
    return BookingService(db).handle_booking_action(booking.id, cg.id, BookingActionRequest(action="complete"))


def test_completing_the_visit_queues_the_difference_for_finance(db, patient):
    booking, cg = _in_progress_with_credit(db, patient)
    _complete(db, booking, cg)
    b = reload(db, booking)
    refunds = db.query(Refund).filter_by(booking_id=booking.id).all()
    assert len(refunds) == 1
    r = refunds[0]
    assert (r.status, r.origin, r.reason_code, r.suggested_amount_kobo, r.amount_kobo) == (
        "pending_approval", "system", "other", 200_000, None)       # a person approves; nothing is paid by itself
    assert "Reschedule price difference" in r.note
    assert (b.refund_required, b.refund_required_reason, b.refund_amount_kobo) == (True, resched.REASON_PRICE_DIFFERENCE, 200_000)


def test_completing_with_nothing_owed_queues_nothing(db, patient):
    booking, cg = _in_progress_with_credit(db, patient, per_hour=250_000)    # quote = paid
    _complete(db, booking, cg)
    assert db.query(Refund).filter_by(booking_id=booking.id).count() == 0 and reload(db, booking).refund_required is False


def test_the_queued_amount_never_exceeds_what_is_left_to_refund(db, patient):
    booking, cg = _in_progress_with_credit(db, patient)
    db.add(Refund(booking_id=booking.id, patient_id=patient.id, reason_code="other", origin="system", status="succeeded",
                  suggested_amount_kobo=950_000, amount_kobo=950_000))
    db.commit()
    _complete(db, booking, cg)
    queued = db.query(Refund).filter_by(booking_id=booking.id, status="pending_approval").one()
    assert queued.suggested_amount_kobo == 50_000       # refundable_kobo: 1,000,000 paid minus 950,000 already refunded


def test_an_admin_marking_the_visit_completed_queues_it_too(db, client, patient):
    booking, cg = _in_progress_with_credit(db, patient)
    h = auth_headers(make_admin(db, ["super_admin"], password=None))
    r = client.post(f"/api/v1/admin/bookings/{booking.id}/correct-status", headers=h,
                    json={"status": "completed", "reason": "Visit confirmed with the family"})
    assert r.status_code == 200, r.text
    assert db.query(Refund).filter_by(booking_id=booking.id, status="pending_approval").one().suggested_amount_kobo == 200_000


def test_a_cancel_after_a_reschedule_refunds_from_the_paid_amount_so_the_credit_is_inside_it(db, patient):
    from app.services import patient_booking_cancel
    booking = paid(db, patient, total_amount_kobo=PAID)
    prices(db, 200_000)
    go(db, patient, booking, hours(60), now=at(0))
    patient_booking_cancel.cancel(db, patient.user, booking.id, now=at(5))
    refunds = db.query(Refund).filter_by(booking_id=booking.id).all()
    assert [r.suggested_amount_kobo for r in refunds] == [PAID]   # the full amount paid, no second refund for the credit


# --- 2. the dispatch run restarts at a reschedule --------------------------------------------------------------------------
def _old_wave(db, booking, caregivers, sent_at):
    db.add(BookingDispatchWave(booking_id=booking.id, wave_no=1, notified_caregiver_ids=[c.id for c in caregivers],
                               sent_at=sent_at, expires_at=sent_at + timedelta(minutes=10)))
    db.commit()


def test_someone_asked_about_the_old_time_is_asked_again_for_the_new_one(db, patient, monkeypatch):
    monkeypatch.setattr(settings, "DISPATCH_WAVE_SIZE", 1)
    c0, c1, c2 = (make_candidate(db, i, at=NOW) for i in range(3))     # c0 is the closest, then c1, then c2
    booking = paid(db, patient, dispatch_state="dispatching", dispatch_started_at=hours(-1))
    _old_wave(db, booking, [c1], hours(-1))                              # the OLD time's first wave asked c1
    go(db, patient, booking, hours(60), now=NOW)
    assert [w.notified_caregiver_ids for w in waves(db, booking)][-1] == [c0.id]       # the reschedule's wave
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=11))                           # it expired: the next wave
    last = waves(db, booking)[-1]
    assert (last.wave_no, last.notified_caregiver_ids) == (3, [c1.id])               # c1 is eligible for the NEW time
    assert reload(db, booking).dispatch_state == "dispatching"


def test_old_waves_do_not_make_the_booking_unmatched_when_nobody_is_online(db, patient):
    booking = paid(db, patient, dispatch_state="dispatching", dispatch_started_at=hours(-1))
    _old_wave(db, booking, [make_caregiver(db)], hours(-1))
    go(db, patient, booking, hours(60), now=NOW)
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=11))
    assert reload(db, booking).dispatch_state == "dispatching"           # nobody has been asked for the new time yet
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=31))              # the usual 30 minutes, then the admin queue
    assert reload(db, booking).dispatch_state == "unmatched"


def test_wave_numbers_stay_unique_across_the_reschedule(db, patient, monkeypatch):
    monkeypatch.setattr(settings, "DISPATCH_WAVE_SIZE", 1)
    for i in range(3):
        make_candidate(db, i, at=NOW)
    booking = paid(db, patient, dispatch_state="dispatching", dispatch_started_at=hours(-1))
    _old_wave(db, booking, [], hours(-1))
    go(db, patient, booking, hours(60), now=NOW)
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=11))
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=22))
    assert [w.wave_no for w in waves(db, booking)] == [1, 2, 3, 4]


# --- 3. a decline stays final ------------------------------------------------------------------------------------------------
def test_a_caregiver_who_declined_is_not_re_offered_by_the_reschedule_or_by_later_waves(db, patient):
    decliner = make_candidate(db, 0, at=NOW)
    other = make_candidate(db, 1, at=NOW)
    booking = paid(db, patient)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=decliner.id, title="t", message="m",
                               notification_type="new_booking", response="declined"))
    db.commit()
    go(db, patient, booking, hours(60), now=NOW)
    assert [w.notified_caregiver_ids for w in waves(db, booking)] == [[other.id]]
    offer = db.query(BookingNotification).filter_by(booking_id=booking.id, caregiver_id=decliner.id).one()
    assert offer.response == "declined"                                  # not reset to unanswered
    dispatch.dispatch_tick(db, NOW + timedelta(minutes=11))              # a later wave must not pick them up either
    assert all(decliner.id not in w.notified_caregiver_ids for w in waves(db, booking))


def test_an_admin_redispatch_that_names_previously_notified_may_still_re_offer_a_decliner(db, patient):
    decliner = make_candidate(db, 0, at=NOW)
    booking = paid(db, patient)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=decliner.id, title="t", message="m",
                               notification_type="new_booking", response="declined"))
    db.commit()
    offers = []
    dispatch.send_wave(db, booking, 1, NOW, offers, renotify=True)       # the admin path is unchanged
    assert offers and offers[0]["caregiver_ids"] == [decliner.id]


# --- found while running the races: an id the server has not seen, for a time another tap already set ----------------------
def test_a_new_request_id_for_the_time_another_tap_already_set_is_a_replay(db, patient):
    booking = paid(db, patient)
    go(db, patient, booking, hours(60), now=at(0))                        # the first tap, sent without an id
    again = go(db, patient, booking, hours(60), now=at(1), request_id="second-device")
    assert again.already is True and reload(db, booking).reschedule_count == 1 and len(history(db, booking)) == 1
