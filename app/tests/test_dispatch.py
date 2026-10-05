"""E4: skills-based matching and wave dispatch.

Job bodies are called directly with an explicit ``now`` (no sleeping, no Redis).
"""

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models.booking import (
    Booking,
    BookingDispatchWave,
    BookingNotification,
    BookingStatus,
    CaregiverLocation,
    PaymentStatus,
)
from app.db.models.caregiver import CaregiverSkill
from app.db.session import SessionLocal
from app.services import dispatch
from app.services.booking_payment import SettleOutcome, settle_booking_payment
from app.services.dispatch import dispatch_tick, find_candidates, start_dispatch
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

NOW = datetime(2026, 10, 5, 12, 0, 0)
LAT, LON = 6.4474, 3.4723  # make_booking's patient location
ACTION = "/api/v1/booking/caregiver/action"


def make_candidate(db, i=0, *, service_type="nurse", skill="nurse", approved=True, suspended=False,
                   available=True, age_minutes=5, lat_offset=None, at=NOW, **cg_overrides):
    """A caregiver who passes every matching filter unless a keyword breaks one."""
    if suspended:
        cg_overrides.update(profile_status="suspended", suspended_at=NOW)
    cg = make_caregiver(db, approved=approved, **cg_overrides)
    if skill:
        db.add(CaregiverSkill(caregiver_id=cg.id, service_type=skill))
    offset = 0.002 * (i + 1) if lat_offset is None else lat_offset  # ~0.2 km per step
    db.add(CaregiverLocation(
        caregiver_id=cg.id, latitude=LAT + offset, longitude=LON, is_active=True,
        is_available=available, updated_at=at - timedelta(minutes=age_minutes)))
    db.commit()
    return cg


def paid_booking(db, **kw):
    return make_booking(db, make_patient(db), payment_status=PaymentStatus.PAID, **kw)


def ids(candidates):
    return [c["caregiver_id"] for c in candidates]


def fresh(db, booking):
    db.expire_all()
    return db.get(Booking, booking.id)


def waves(db, booking):
    return (db.query(BookingDispatchWave).filter_by(booking_id=booking.id)
            .order_by(BookingDispatchWave.wave_no).all())


def notified_ids(db, booking):
    return [n.caregiver_id for n in db.query(BookingNotification).filter_by(booking_id=booking.id)]


# --- candidate filtering: one test per filter -----------------------------------


def test_eligible_caregiver_is_a_candidate(db):
    cg = make_candidate(db)
    assert ids(find_candidates(db, paid_booking(db), NOW)) == [cg.id]


def test_unapproved_caregiver_is_excluded(db):
    make_candidate(db, approved=False)
    assert find_candidates(db, paid_booking(db), NOW) == []


def test_suspended_caregiver_is_excluded(db):
    make_candidate(db, suspended=True)
    assert find_candidates(db, paid_booking(db), NOW) == []


def test_suspended_flag_excludes_even_if_status_still_approved(db):
    make_candidate(db, suspended_at=NOW)
    assert find_candidates(db, paid_booking(db), NOW) == []


def test_caregiver_without_any_skill_is_excluded(db):
    make_candidate(db, skill=None)  # specialization text says 'nurse', but there is no skill row
    assert find_candidates(db, paid_booking(db), NOW) == []


def test_caregiver_with_a_different_skill_is_excluded(db):
    make_candidate(db, skill="doctor", specialization="nurse")
    assert find_candidates(db, paid_booking(db, service_type="nurse"), NOW) == []


def test_free_text_specialization_no_longer_matches(db):
    # skill row says doctor; the free-text field claims nurse. Only the skill row counts.
    cg = make_candidate(db, skill="doctor", specialization="nurse")
    assert ids(find_candidates(db, paid_booking(db, service_type="doctor"), NOW)) == [cg.id]


def test_stale_location_is_excluded(db):
    make_candidate(db, age_minutes=settings.LOCATION_FRESHNESS_MINUTES + 1)
    assert find_candidates(db, paid_booking(db), NOW) == []


def test_location_at_the_freshness_limit_is_still_fresh(db):
    cg = make_candidate(db, age_minutes=settings.LOCATION_FRESHNESS_MINUTES)
    assert ids(find_candidates(db, paid_booking(db), NOW)) == [cg.id]


def test_unavailable_caregiver_is_excluded(db):
    make_candidate(db, available=False)
    assert find_candidates(db, paid_booking(db), NOW) == []


def test_caregiver_beyond_20km_is_excluded_and_inside_is_included(db):
    far = make_candidate(db, lat_offset=0.25)  # ~27.8 km
    near = make_candidate(db, lat_offset=0.15)  # ~16.7 km
    assert ids(find_candidates(db, paid_booking(db), NOW)) == [near.id]
    assert far.id not in ids(find_candidates(db, paid_booking(db), NOW))


def test_caregiver_with_null_specialization_does_not_break_scoring(db):
    cg = make_candidate(db, specialization=None)
    assert ids(find_candidates(db, paid_booking(db), NOW)) == [cg.id]


def test_stationary_caregiver_stays_matchable_when_pinging_identical_coordinates(db):
    from app.services.location import LocationService

    cg = make_caregiver(db)
    db.add(CaregiverSkill(caregiver_id=cg.id, service_type="nurse"))
    svc = LocationService(db)
    svc.update_caregiver_location(cg.id, LAT + 0.002, LON)
    loc = db.query(CaregiverLocation).filter_by(caregiver_id=cg.id).one()
    loc.updated_at = datetime.utcnow() - timedelta(minutes=settings.LOCATION_FRESHNESS_MINUTES + 5)
    db.commit()  # time passes; the first fix is now stale
    assert find_candidates(db, paid_booking(db), datetime.utcnow()) == []

    svc.update_caregiver_location(cg.id, LAT + 0.002, LON)  # same GPS fix: no column value changes
    db.expire_all()
    assert ids(find_candidates(db, paid_booking(db), datetime.utcnow())) == [cg.id]


def test_closer_caregiver_ranks_first(db):
    far = make_candidate(db, lat_offset=0.08)
    near = make_candidate(db, lat_offset=0.004)
    assert ids(find_candidates(db, paid_booking(db), NOW)) == [near.id, far.id]


# --- starting dispatch -----------------------------------------------------------


def test_start_dispatch_sends_wave_one_of_eight(db):
    cgs = [make_candidate(db, i) for i in range(12)]
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)

    b = fresh(db, b)
    assert b.dispatch_state == "dispatching"
    assert b.dispatch_started_at == NOW
    (w1,) = waves(db, b)
    assert w1.wave_no == 1 and w1.sent_at == NOW
    assert w1.expires_at == NOW + timedelta(minutes=settings.DISPATCH_WAVE_MINUTES)
    assert len(w1.notified_caregiver_ids) == settings.DISPATCH_WAVE_SIZE == 8
    assert set(w1.notified_caregiver_ids) <= {c.id for c in cgs}
    assert sorted(notified_ids(db, b)) == sorted(w1.notified_caregiver_ids)


def test_start_dispatch_twice_sends_one_wave(db):
    for i in range(3):
        make_candidate(db, i)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    start_dispatch(db, b.id, NOW + timedelta(minutes=1))
    assert len(waves(db, b)) == 1
    assert len(notified_ids(db, b)) == 3


def test_no_candidates_yet_keeps_dispatching_and_wave_one_goes_out_when_one_appears(db):
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    assert fresh(db, b).dispatch_state == "dispatching" and waves(db, b) == []
    assert tick(db, minutes(1)) == 0
    assert fresh(db, b).dispatch_state == "dispatching"

    cg = make_candidate(db)
    assert tick(db, minutes(2)) == 1
    (w1,) = waves(db, b)
    assert w1.wave_no == 1 and w1.notified_caregiver_ids == [cg.id] and w1.sent_at == minutes(2)


def test_no_candidates_for_the_whole_window_becomes_unmatched(db, caplog):
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    tick(db, minutes(29))
    assert fresh(db, b).dispatch_state == "dispatching"
    with caplog.at_level(logging.WARNING):
        tick(db, minutes(30))
    assert fresh(db, b).dispatch_state == "unmatched"
    assert waves(db, b) == []
    assert "unmatched" in caplog.text and str(b.id) in caplog.text


# --- settlement starts dispatch exactly once -------------------------------------


def _webhook(client, b, kobo=4_400_000):
    body = json.dumps({"event": "charge.success", "data": {
        "reference": b.paystack_reference, "amount": kobo, "status": "success"}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/v1/payment/paystack/webhook", content=body, headers={"x-paystack-signature": sig})


def test_settlement_starts_wave_one_and_replays_do_not_repeat_it(db, client, monkeypatch):
    for i in range(10):
        make_candidate(db, i, at=datetime.utcnow())  # settlement runs on the real clock
    patient = make_patient(db)
    b = make_booking(db, patient, payment_status=PaymentStatus.PENDING, total_amount_kobo=4_400_000)
    monkeypatch.setattr(PaystackService, "verify_transaction", lambda self, ref: {
        "status": True, "data": {"id": 1, "reference": ref, "amount_kobo": 4_400_000, "status": "success",
                                 "paid_at": "2026-10-05T10:00:00.000Z"}})

    assert _webhook(client, b).status_code == 200
    assert _webhook(client, b).status_code == 200  # replay
    r = client.post(f"/api/v1/booking/booking/{b.id}/verify-payment", headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text  # parity path, already paid

    b = fresh(db, b)
    assert b.payment_status == PaymentStatus.PAID
    assert b.dispatch_state == "dispatching"
    assert len(waves(db, b)) == 1
    assert len(notified_ids(db, b)) == 8


def test_settle_function_called_twice_dispatches_once(db):
    make_candidate(db, at=datetime.utcnow())  # settlement runs on the real clock
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, total_amount_kobo=4_400_000)
    assert settle_booking_payment(db, b.id, 4_400_000) == SettleOutcome.PAID
    assert settle_booking_payment(db, b.id, 4_400_000) == SettleOutcome.ALREADY_PAID
    assert len(waves(db, b)) == 1 and len(notified_ids(db, b)) == 1


def test_wave_failure_after_payment_is_recovered_by_the_tick(db, monkeypatch):
    make_candidate(db)
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, total_amount_kobo=4_400_000)

    def boom(*a, **kw):
        raise RuntimeError("push provider down")

    monkeypatch.setattr(dispatch, "start_dispatch", boom)
    assert settle_booking_payment(db, b.id, 4_400_000) == SettleOutcome.PAID
    b = fresh(db, b)
    assert b.payment_status == PaymentStatus.PAID and b.dispatch_state == "dispatching"
    assert waves(db, b) == []

    dispatch_tick(db, NOW)  # state is committed with the payment, so the tick can pick it up
    assert len(waves(db, b)) == 1


def test_amount_mismatch_does_not_start_dispatch(db):
    make_candidate(db)
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, total_amount_kobo=4_400_000)
    assert settle_booking_payment(db, b.id, 1) == SettleOutcome.AMOUNT_MISMATCH
    b = fresh(db, b)
    assert b.dispatch_state is None and waves(db, b) == []


# --- wave progression ------------------------------------------------------------


def minutes(n):
    return NOW + timedelta(minutes=n)


def tick(session, now):
    """A tick at ``now``, with every caregiver app having reported its location a minute earlier."""
    session.query(CaregiverLocation).update({"updated_at": now - timedelta(minutes=1)})
    session.commit()
    return dispatch_tick(session, now)


def test_tick_before_the_wave_expires_does_nothing(db):
    for i in range(20):
        make_candidate(db, i)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    assert tick(db, minutes(9)) == 0
    assert len(waves(db, b)) == 1


def test_next_wave_goes_to_not_yet_notified_candidates(db):
    for i in range(20):
        make_candidate(db, i)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    assert tick(db, minutes(10)) == 1
    w1, w2 = waves(db, b)
    assert w2.wave_no == 2 and w2.sent_at == minutes(10)
    assert w2.expires_at == minutes(20)
    assert len(w2.notified_caregiver_ids) == 8
    assert not set(w1.notified_caregiver_ids) & set(w2.notified_caregiver_ids)
    assert len(notified_ids(db, b)) == 16
    assert fresh(db, b).dispatch_state == "dispatching"


def test_tick_is_idempotent_for_the_same_instant(db):
    for i in range(20):
        make_candidate(db, i)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    tick(db, minutes(10))
    assert tick(db, minutes(10)) == 0
    assert len(waves(db, b)) == 2 and len(notified_ids(db, b)) == 16


def test_escalates_to_unmatched_after_30_minutes(db, caplog):
    for i in range(30):
        make_candidate(db, i)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    tick(db, minutes(10))
    tick(db, minutes(20))
    assert fresh(db, b).dispatch_state == "dispatching"
    assert len(waves(db, b)) == 3

    with caplog.at_level(logging.WARNING):
        tick(db, minutes(30))
    b = fresh(db, b)
    assert b.dispatch_state == "unmatched"
    assert len(waves(db, b)) == 3  # no fourth wave although candidates remain
    assert "unmatched" in caplog.text and str(b.id) in caplog.text
    assert tick(db, minutes(40)) == 0  # terminal


def test_escalates_early_when_candidates_are_exhausted(db, caplog):
    for i in range(10):
        make_candidate(db, i)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    tick(db, minutes(10))  # the remaining 2
    assert len(waves(db, b)[1].notified_caregiver_ids) == 2
    assert fresh(db, b).dispatch_state == "dispatching"

    with caplog.at_level(logging.WARNING):
        tick(db, minutes(20))  # nobody left
    b = fresh(db, b)
    assert b.dispatch_state == "unmatched"
    assert len(waves(db, b)) == 2
    assert "unmatched" in caplog.text


def test_candidates_who_went_stale_between_waves_are_not_notified(db):
    for i in range(8):
        make_candidate(db, i)
    late = make_candidate(db, 8)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    loc = db.query(CaregiverLocation).filter_by(caregiver_id=late.id).one()
    loc.updated_at = NOW - timedelta(minutes=60)
    db.commit()
    dispatch_tick(db, minutes(10))
    assert fresh(db, b).dispatch_state == "unmatched"
    assert late.id not in notified_ids(db, b)


def test_tick_skips_cancelled_and_assigned_bookings(db):
    for i in range(20):
        make_candidate(db, i)
    cancelled = paid_booking(db)
    start_dispatch(db, cancelled.id, NOW)
    cancelled = fresh(db, cancelled)
    cancelled.status = BookingStatus.CANCELLED
    db.commit()
    assert tick(db, minutes(15)) == 0
    assert len(waves(db, cancelled)) == 1


def test_tick_ignores_legacy_bookings_without_dispatch_state(db):
    make_candidate(db)
    b = paid_booking(db)  # dispatch_state NULL: paid before this feature existed
    assert dispatch_tick(db, NOW) == 0
    assert waves(db, b) == []


def test_unique_wave_number_per_booking(db):
    b = paid_booking(db)
    db.add(BookingDispatchWave(booking_id=b.id, wave_no=1, notified_caregiver_ids=[],
                               sent_at=NOW, expires_at=NOW))
    db.commit()
    db.add(BookingDispatchWave(booking_id=b.id, wave_no=1, notified_caregiver_ids=[],
                               sent_at=NOW, expires_at=NOW))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --- two workers ---------------------------------------------------------------


def test_two_concurrent_ticks_do_not_double_notify(db):
    for i in range(20):
        make_candidate(db, i)
    bookings = [paid_booking(db) for _ in range(3)]
    for b in bookings:
        start_dispatch(db, b.id, NOW)

    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            results.append(dispatch_tick(session, minutes(10)))
        except Exception as exc:  # surfaced below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert sum(results) == 3  # each booking advanced exactly once across both workers
    for b in bookings:
        ws = waves(db, b)
        assert [w.wave_no for w in ws] == [1, 2]
        notified = notified_ids(db, b)
        assert len(notified) == len(set(notified)) == 16


def test_tick_skips_a_booking_another_worker_holds_locked(db):
    make_candidate(db)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)

    holder = SessionLocal()
    try:
        holder.query(Booking).filter(Booking.id == b.id).with_for_update().first()
        assert tick(db, minutes(15)) == 0  # skipped, not blocked
    finally:
        holder.rollback()
        holder.close()


# --- accepting -------------------------------------------------------------------


def _accept(client, cg, booking):
    return client.post(f"{ACTION}/{booking.id}", json={"action": "accept"}, headers=auth_headers(cg.user))


def test_accept_sets_matched(db, client):
    cg = make_candidate(db)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    assert _accept(client, cg, b).status_code == 200
    b = fresh(db, b)
    assert b.dispatch_state == "matched" and b.caregiver_id == cg.id
    assert b.status == BookingStatus.ACCEPTED


def test_caregiver_from_wave_one_can_accept_after_wave_two_went_out(db, client):
    cgs = [make_candidate(db, i) for i in range(12)]
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    tick(db, minutes(10))
    w1_ids = waves(db, b)[0].notified_caregiver_ids
    first = next(c for c in cgs if c.id in w1_ids)
    assert _accept(client, first, b).status_code == 200
    b = fresh(db, b)
    assert b.dispatch_state == "matched"
    others = db.query(BookingNotification).filter(
        BookingNotification.booking_id == b.id, BookingNotification.caregiver_id != first.id).all()
    assert len(others) == 11 and {n.response for n in others} == {"auto_declined"}


def test_caregiver_can_still_accept_after_the_booking_was_escalated(db, client):
    cg = make_candidate(db)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    tick(db, minutes(10))
    assert fresh(db, b).dispatch_state == "unmatched"

    assert _accept(client, cg, b).status_code == 200
    b = fresh(db, b)
    assert b.dispatch_state == "matched" and b.caregiver_id == cg.id


def test_tick_does_not_resume_dispatch_after_a_late_accept(db, client):
    cgs = [make_candidate(db, i) for i in range(12)]
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    first = next(c for c in cgs if c.id in waves(db, b)[0].notified_caregiver_ids)
    assert _accept(client, first, b).status_code == 200
    assert tick(db, minutes(15)) == 0
    assert len(waves(db, b)) == 1


def test_accept_of_a_cancelled_booking_is_still_refused(db, client):
    cg = make_candidate(db)
    b = paid_booking(db)
    start_dispatch(db, b.id, NOW)
    b = fresh(db, b)
    b.status = BookingStatus.CANCELLED
    db.commit()
    assert _accept(client, cg, b).status_code == 409
