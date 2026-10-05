"""C3 item 2: a dispatch wave pushes "new visit request" to the caregivers it notified."""

import logging
from datetime import timedelta

import httpx

from app.core.config import settings
from app.db.models.booking import BookingDispatchWave, BookingNotification
from app.db.models.push_token import PushToken
from app.db.session import SessionLocal
from app.services import push
from app.services.dispatch import dispatch_tick, start_dispatch
from app.tests.factories import make_booking, make_patient
from app.tests.test_dispatch import NOW, fresh, make_candidate, notified_ids, waves


def with_token(db, cg, n=0):
    token = f"ExponentPushToken[cg-{cg.id.hex[:8]}-{n}]"
    db.add(PushToken(user_id=cg.user_id, token=token, platform="ios"))
    db.commit()
    return token


def sensitive_booking(db, **kw):
    patient = make_patient(db, full_name="Zainab Okonkwo-Placeholder")
    return make_booking(
        db, patient, condition="Placeholder-Condition-HIV", special_instructions="Gate code 0000",
        assistance_type="placeholder-assistance", patient_address="12 Placeholder Street, Lekki",
        patient_city="Lagos", patient_state="Lagos", **kw)


def test_wave_one_pushes_each_notified_caregiver_once(db, expo):
    cgs = [make_candidate(db, i) for i in range(3)]
    tokens = {with_token(db, c) for c in cgs}
    booking = sensitive_booking(db)

    start_dispatch(db, booking.id, NOW)

    assert {m["to"] for m in expo.messages} == tokens
    assert len(expo.messages) == 3
    assert len(notified_ids(db, booking)) == 3


def test_payload_is_minimal_and_carries_no_patient_information(db, expo):
    cg = make_candidate(db)
    with_token(db, cg)
    booking = sensitive_booking(db)

    start_dispatch(db, booking.id, NOW)

    msg = expo.messages[0]
    assert msg["title"] == "New visit request"
    assert msg["data"] == {
        "type": "job_offer",
        "booking_id": str(booking.id),
        "area": "Lagos",
        "scheduled_at": booking.scheduled_date.isoformat() + "Z",
    }
    assert "Lagos" in msg["body"]
    body = expo.raw_bodies
    for secret in ("Zainab", "Okonkwo", "Placeholder-Condition", "HIV", "Gate code", "Placeholder Street",
                   "placeholder-assistance", booking.patient.phone_number, booking.patient.email):
        assert secret not in body, secret
    assert booking.booking_code not in body


def test_only_caregivers_with_tokens_are_pushed_but_all_are_offered(db, expo):
    with_tok, without = make_candidate(db, 0), make_candidate(db, 1)
    token = with_token(db, with_tok)
    booking = sensitive_booking(db)

    start_dispatch(db, booking.id, NOW)

    assert [m["to"] for m in expo.messages] == [token]
    assert set(notified_ids(db, booking)) == {with_tok.id, without.id}


def test_push_happens_after_the_wave_is_committed(db, expo):
    cg = make_candidate(db)
    with_token(db, cg)
    booking = sensitive_booking(db)
    seen = {}

    def check(request, batch):
        with SessionLocal() as other:  # a separate connection only sees committed rows
            seen["waves"] = other.query(BookingDispatchWave).filter_by(booking_id=booking.id).count()
            seen["notifications"] = other.query(BookingNotification).filter_by(booking_id=booking.id).count()

    expo.on_request = check
    start_dispatch(db, booking.id, NOW)
    assert seen == {"waves": 1, "notifications": 1}


def test_a_later_wave_pushes_only_the_caregivers_newly_notified(db, expo, monkeypatch):
    monkeypatch.setattr(settings, "DISPATCH_WAVE_SIZE", 2)
    cgs = [make_candidate(db, i, age_minutes=0) for i in range(4)]  # still fresh at wave 2
    for c in cgs:
        with_token(db, c)
    booking = sensitive_booking(db)

    start_dispatch(db, booking.id, NOW)
    first = {m["to"] for m in expo.messages}
    dispatch_tick(db, NOW + timedelta(minutes=settings.DISPATCH_WAVE_MINUTES, seconds=1))

    all_sent = [m["to"] for m in expo.messages]
    assert len(all_sent) == 4 and len(set(all_sent)) == 4  # nobody pushed twice
    assert len(first) == 2


def test_a_tick_that_changes_nothing_pushes_nothing(db, expo):
    cg = make_candidate(db)
    with_token(db, cg)
    booking = sensitive_booking(db)
    start_dispatch(db, booking.id, NOW)
    expo.batches.clear()

    dispatch_tick(db, NOW + timedelta(minutes=1))  # wave 1 still open
    start_dispatch(db, booking.id, NOW + timedelta(minutes=1))  # replay
    assert expo.messages == []


def test_a_failing_push_does_not_disturb_dispatch_and_is_not_retried_as_a_resend(db, expo, caplog):
    cgs = [make_candidate(db, i) for i in range(2)]
    for c in cgs:
        with_token(db, c)
    expo.raise_error = httpx.ConnectError("expo is down")
    booking = sensitive_booking(db)

    with caplog.at_level(logging.WARNING):
        start_dispatch(db, booking.id, NOW)
    after_first = fresh(db, booking)
    assert after_first.dispatch_state == "dispatching"
    assert len(waves(db, booking)) == 1
    assert len(notified_ids(db, booking)) == 2
    assert any("push" in r.getMessage().lower() for r in caplog.records)

    # the worker tick and a replay must not send the wave again
    dispatch_tick(db, NOW + timedelta(minutes=1))
    start_dispatch(db, booking.id, NOW + timedelta(minutes=1))
    assert len(waves(db, booking)) == 1
    assert len(notified_ids(db, booking)) == 2


def test_a_push_service_that_blows_up_is_contained(db, expo, monkeypatch):
    cg = make_candidate(db)
    with_token(db, cg)

    def boom(*a, **k):
        raise RuntimeError("push exploded")

    monkeypatch.setattr(push.PushService, "send_to_users", boom)
    booking = sensitive_booking(db)
    start_dispatch(db, booking.id, NOW)  # must not raise
    assert len(waves(db, booking)) == 1 and fresh(db, booking).dispatch_state == "dispatching"


def test_the_tick_keeps_going_when_a_push_fails_for_one_booking(db, expo):
    first_cg = make_candidate(db, 0)
    with_token(db, first_cg)
    b1 = sensitive_booking(db)
    start_dispatch(db, b1.id, NOW)  # wave 1 sent, push succeeded
    expo.raise_error = httpx.ReadTimeout("slow")

    second_cg = make_candidate(db, 1, at=NOW + timedelta(minutes=11))
    with_token(db, second_cg)
    b2 = sensitive_booking(db)
    # b2 is paid and 'dispatching' but has no wave: the tick sends wave 1 and the push fails
    b2.dispatch_state, b2.dispatch_started_at = "dispatching", NOW + timedelta(minutes=11)
    db.commit()

    dispatch_tick(db, NOW + timedelta(minutes=11))
    assert len(waves(db, b2)) == 1  # advanced despite the failed push
    assert fresh(db, b1).dispatch_state == "dispatching"


def test_unavailable_caregivers_are_not_offered_or_pushed(db, expo):
    on, off = make_candidate(db, 0), make_candidate(db, 1, available=False)
    with_token(db, on)
    off_token = with_token(db, off)
    booking = sensitive_booking(db)

    start_dispatch(db, booking.id, NOW)

    assert off_token not in {m["to"] for m in expo.messages}
    assert notified_ids(db, booking) == [on.id]


def test_a_dead_token_found_during_dispatch_is_pruned(db, expo):
    cg = make_candidate(db)
    token = with_token(db, cg)
    expo.not_registered = {token}
    start_dispatch(db, sensitive_booking(db).id, NOW)
    db.expire_all()
    assert db.query(PushToken).count() == 0
