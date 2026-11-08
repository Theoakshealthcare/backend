"""Slice 2 of 2026-11-07: the caregiver's own earning on their booking views, the "I've arrived" action, the short-visit
threshold as booking policy, and the family's cancel reason stored on the booking."""

import json
import uuid
from datetime import datetime, timedelta

import pytest

from app.api.v1.endpoints.booking import attach_caregiver_earning, enrich_booking_with_details
from app.core.money import kobo_to_naira
from app.db.models.booking import Booking, BookingNotification, BookingStatus, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.db.models.whatsapp_booking import BookingEvent
from app.schemas.booking import BookingCreate
from app.services import dispatch, dropout, earnings
from app.services.booking import BookingService
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient, make_user
from app.tests.policy_helpers import set_policy
from app.tests.test_caregiver_earnings import complete, txs
from app.tests.test_patient_notifications import (  # noqa: F401  (fixtures)
    app_booking, deliver, events, feed, user_of, with_token, world,
)
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

API = "/api/v1/booking"
ACTION = f"{API}/caregiver/action"
CREATE = dict(service_type="nurse", assistance_type="x", duration_hours=3, scheduled_date="2030-01-01T10:00:00",
              patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
              patient_state="Lagos", booking_for="myself")
TERMS = {"visit_share_percent": 70, "hold_hours": 24, "late_fee_share_percent": 80, "short_visit_min_percent": 50}


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)


def act(client, booking, cg, action, **extra):
    return client.post(f"{ACTION}/{booking.id}", json={"action": action, **extra}, headers=auth_headers(cg.user))


def reload(db, booking) -> Booking:
    db.expire_all()
    return db.get(Booking, booking.id)


def accepted(db, cg, **kw):
    kw.setdefault("earnings_terms", TERMS)
    return make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, **kw)


# === (a) the caregiver's own earning ======================================================================================
def expected(booking, percent=70):
    return float(kobo_to_naira(earnings.share(booking.service_fee_kobo, percent)))


def test_the_assigned_caregiver_sees_their_earning_on_detail_list_action_and_offer_list(db, client):
    cg = make_caregiver(db)
    b = accepted(db, cg)
    h = auth_headers(cg.user)
    db.add(BookingNotification(booking_id=b.id, caregiver_id=cg.id, title="t", message="m", notification_type="new_booking",
                               response="accept"))
    db.commit()
    detail = client.get(f"{API}/booking/{b.id}", headers=h).json()
    listed = client.get(f"{API}/caregiver/bookings", headers=h).json()
    feed_item = client.get(f"{API}/caregiver/notifications", headers=h).json()[0]["booking"]
    acted = act(client, b, cg, "arrived").json()["booking"]
    assert detail["caregiver_earning"] == listed[0]["caregiver_earning"] == feed_item["caregiver_earning"] == expected(b)
    assert acted["caregiver_earning"] == expected(b) and expected(b) > 0


def test_the_earning_comes_from_the_snapshot_not_the_live_policy(db, client):
    cg = make_caregiver(db)
    b = accepted(db, cg)                                  # snapshot: 70 %
    set_policy(db, caregiver_visit_share_percent=95)      # a later edit
    r = client.get(f"{API}/booking/{b.id}", headers=auth_headers(cg.user))
    assert r.json()["caregiver_earning"] == expected(b, 70) != expected(b, 95)


def test_the_earning_shown_is_exactly_what_completing_the_visit_credits(db, client):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.IN_PROGRESS, earnings_terms=TERMS,
                     actual_start_time=datetime.utcnow() - timedelta(hours=3), service_fee_kobo=1_234_569)
    shown = client.get(f"{API}/booking/{b.id}", headers=auth_headers(cg.user)).json()["caregiver_earning"]
    assert complete(client, b, cg).status_code == 200
    [tx] = txs(db, cg, type="earning")
    assert float(kobo_to_naira(tx.amount)) == shown


def test_no_snapshot_means_no_number_not_a_guess_from_the_live_policy(db, client):
    cg = make_caregiver(db)
    for terms in (None, {"visit_share_percent": "lots"}):
        b = accepted(db, cg, earnings_terms=terms)
        assert client.get(f"{API}/booking/{b.id}", headers=auth_headers(cg.user)).json()["caregiver_earning"] is None


@pytest.mark.parametrize("status, shown", [
    (BookingStatus.ACCEPTED, True), (BookingStatus.EN_ROUTE, True), (BookingStatus.IN_PROGRESS, True),
    (BookingStatus.COMPLETED, True), (BookingStatus.CANCELLED, False), (BookingStatus.PENDING, False)])
def test_the_earning_is_shown_only_while_it_can_still_be_earned_or_was(db, client, status, shown):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), cg, status=status, earnings_terms=TERMS)
    body = client.get(f"{API}/caregiver/bookings", headers=auth_headers(cg.user)).json()[0]
    assert (body["caregiver_earning"] is not None) is shown


def test_the_earning_never_reaches_patients_admins_or_other_caregivers(db, client):
    cg, other = make_caregiver(db), make_caregiver(db)
    patient = make_patient(db)
    b = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, earnings_terms=TERMS, visit_code="1234")
    db.add(BookingNotification(booking_id=b.id, caregiver_id=other.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    assert client.get(f"{API}/booking/{b.id}", headers=auth_headers(patient.user)).json()["caregiver_earning"] is None
    assert client.get(f"{API}/patient/bookings", headers=auth_headers(patient.user)).json()[0]["caregiver_earning"] is None
    admin = make_admin(db, ["super_admin"], password=None)
    assert client.get(f"{API}/booking/{b.id}", headers=auth_headers(admin)).json()["caregiver_earning"] is None
    offered = client.get(f"{API}/caregiver/notifications", headers=auth_headers(other.user)).json()[0]["booking"]
    assert offered["caregiver_earning"] is None
    assert attach_caregiver_earning(enrich_booking_with_details(b, db), b, other.id)["caregiver_earning"] is None


# === (b) "I've arrived" ===================================================================================================
def test_arrived_stamps_the_time_keeps_the_status_and_tells_the_family_once(db, expo, world):
    cg = world["caregiver"]
    b = app_booking(db, world["patient"], cg, status=BookingStatus.ACCEPTED, earnings_terms=TERMS)
    from app.tests.test_patient_notifications import act as svc_act
    first = svc_act(db, b, cg, "arrived")
    stamp = first.arrived_at
    assert stamp is not None and first.status == BookingStatus.ACCEPTED
    deliver(db)
    again = svc_act(db, b, cg, "arrived")                       # a double tap or a retry
    assert again.arrived_at == stamp and again.status == BookingStatus.ACCEPTED
    deliver(db)
    assert events(db, b).count("caregiver_arrived") == 1
    rows = feed(db, world["user"])
    assert [r.type for r in rows].count("caregiver_arrived") == 1
    assert [r.body for r in rows if r.type == "caregiver_arrived"] == ["Open the app to see your visit code."]


def test_arrived_works_from_en_route_and_over_http(db, client):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.EN_ROUTE, earnings_terms=TERMS)
    r = act(client, b, cg, "arrived")
    assert r.status_code == 200 and r.json()["message"] == "Booking arrived successfully"
    body = r.json()["booking"]
    assert body["status"] == "en_route" and body["arrived_at"] is not None
    assert act(client, b, cg, "arrived").json()["booking"]["arrived_at"] == body["arrived_at"]
    assert reload(db, b).arrived_at is not None


def test_only_the_assigned_caregiver_may_arrive(db, client):
    cg, stranger, offered = make_caregiver(db), make_caregiver(db), make_caregiver(db)
    b = accepted(db, cg)
    db.add(BookingNotification(booking_id=b.id, caregiver_id=offered.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    for who in (stranger, offered):
        r = act(client, b, who, "arrived")
        assert r.status_code == 403
        assert r.json()["detail"] == {"code": "booking_not_assigned", "message": "This booking is not assigned to you"}
    assert reload(db, b).arrived_at is None
    assert client.post(f"{ACTION}/{b.id}", json={"action": "arrived"}).status_code == 401


@pytest.mark.parametrize("status", [BookingStatus.PENDING, BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED,
                                    BookingStatus.CANCELLED])
def test_arrived_only_on_an_accepted_or_en_route_booking(db, client, status):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), cg, status=status)
    r = act(client, b, cg, "arrived")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "booking_not_arrivable",
                                  "message": "Only an accepted booking can be marked as arrived"}
    assert reload(db, b).arrived_at is None


def test_a_suspended_caregiver_cannot_arrive_for_a_visit(db, client):
    cg = make_caregiver(db)
    b = accepted(db, cg)
    cg.profile_status = "suspended"
    db.commit()
    r = act(client, b, cg, "arrived")
    assert r.status_code == 403
    assert r.json()["detail"] == {"code": "caregiver_not_approved", "message": "Your account is not approved to start visits"}


def test_arrived_on_an_unknown_booking_is_a_coded_404(db, client):
    cg = make_caregiver(db)
    r = client.post(f"{ACTION}/{uuid.uuid4()}", json={"action": "arrived"}, headers=auth_headers(cg.user))
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "booking_not_found", "message": "Booking not found"}


def test_arrived_without_a_caregiver_profile_is_a_coded_404(db, client):
    user = make_user(db, "caregiver")  # a caregiver login that never created its profile
    r = client.post(f"{ACTION}/{uuid.uuid4()}", json={"action": "arrived"}, headers=auth_headers(user))
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "caregiver_profile_not_found", "message": "Caregiver profile not found"}
    r = client.post(f"{ACTION}/{uuid.uuid4()}", json={"action": "start"}, headers=auth_headers(user))
    assert r.status_code == 404 and r.json()["detail"] == "Caregiver profile not found"


def test_the_other_actions_still_answer_plain_string_errors(db, client):
    """Only ``arrived`` moved to {code, message}: the caregiver app reads the others as strings until it is changed."""
    cg, stranger = make_caregiver(db), make_caregiver(db)
    b = accepted(db, cg)
    r = act(client, b, stranger, "start")
    assert r.status_code == 403 and r.json()["detail"] == "This booking is not assigned to you"
    pending = make_booking(db, make_patient(db), cg, status=BookingStatus.PENDING)
    r = act(client, pending, cg, "start")
    assert r.status_code == 409 and r.json()["detail"] == "Only an accepted booking can be started"


def test_the_family_sees_arrived_at_and_another_caregiver_never_does(db, client):
    cg, other = make_caregiver(db), make_caregiver(db)
    patient = make_patient(db)
    b = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, earnings_terms=TERMS)
    act(client, b, cg, "arrived")
    assert client.get(f"{API}/booking/{b.id}", headers=auth_headers(patient.user)).json()["arrived_at"] is not None
    db.add(BookingNotification(booking_id=b.id, caregiver_id=other.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    offered = client.get(f"{API}/caregiver/notifications", headers=auth_headers(other.user)).json()[0]["booking"]
    assert offered["arrived_at"] is None


def test_a_whatsapp_booking_records_the_arrival_but_sends_nothing_app_only(db, client):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, channel="whatsapp")
    assert act(client, b, cg, "arrived").status_code == 200
    assert reload(db, b).arrived_at is not None
    assert db.query(BookingEvent).filter_by(booking_id=b.id, event="caregiver_arrived").count() == 0


def test_a_new_caregiver_has_not_arrived_yet(db):
    cg = make_caregiver(db)
    b = accepted(db, cg, arrived_at=datetime.utcnow())
    dropout.clear_on_assignment(b)
    assert b.arrived_at is None


def test_starting_the_visit_after_arriving_still_needs_the_code_and_works(db, client):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, visit_code="4321", earnings_terms=TERMS)
    assert act(client, b, cg, "arrived").status_code == 200
    assert act(client, b, cg, "start").status_code == 400
    assert act(client, b, cg, "start", visit_code="4321").status_code == 200
    assert reload(db, b).status == BookingStatus.IN_PROGRESS


# === (c) the short-visit threshold as policy ==============================================================================
def pricing(db):
    db.add(BookingSettings(base_fee_kobo=500_000, platform_fee_percent=10, multipliers={"nurse": 1.0}))
    db.commit()


def test_the_default_is_50_and_a_new_booking_carries_the_policy_value(db):
    pricing(db)
    patient = make_patient(db)
    assert BookingService(db).insert_booking(patient.id, BookingCreate(**CREATE)).earnings_terms[
        "short_visit_min_percent"] == 50
    set_policy(db, short_visit_min_percent=30)
    assert BookingService(db).insert_booking(patient.id, BookingCreate(**CREATE)).earnings_terms == {
        "visit_share_percent": 80, "hold_hours": 24, "late_fee_share_percent": 80, "short_visit_min_percent": 30}


def timed(db, minutes_on_site, **kw):
    cg = make_caregiver(db)
    start = datetime(2026, 11, 7, 10, 0, 0)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, duration_hours=2,
                     actual_start_time=start, actual_end_time=start + timedelta(minutes=minutes_on_site), **kw)
    return b


@pytest.mark.parametrize("snap, minutes, short", [
    (30, 35, True), (30, 36, False), (30, 30, True),                      # 120 min booked: 30 % = 36 min
    (60, 71, True), (60, 72, False), (50, 59, True), (50, 60, False), (0, 0, False), (100, 119, True), (100, 120, False)])
def test_the_booking_s_own_threshold_decides(db, snap, minutes, short):
    terms = {**TERMS, "short_visit_min_percent": snap}
    b = timed(db, minutes, earnings_terms=terms)
    assert earnings.is_short_visit(b, earnings.terms_for(db, b).short_visit_min_percent) is short


def test_a_live_policy_change_never_re_judges_an_existing_booking(db, client):
    cg = make_caregiver(db)
    b = make_booking(db, make_patient(db), cg, status=BookingStatus.IN_PROGRESS, duration_hours=4, earnings_terms=TERMS,
                     actual_start_time=datetime.utcnow() - timedelta(hours=1))      # 25 % on site, snapshot threshold 50
    set_policy(db, short_visit_min_percent=10)                                      # an admin relaxes the rule afterwards
    assert complete(client, b, cg).status_code == 200
    [tx] = txs(db, cg, type="earning")
    assert tx.status == "held" and tx.status_reason == earnings.short_visit_hold_reason(50)


def test_a_threshold_of_30_in_the_snapshot_lets_the_same_visit_clear_and_names_the_percent_when_it_holds(db, client):
    cg = make_caregiver(db)
    ok = make_booking(db, make_patient(db), cg, status=BookingStatus.IN_PROGRESS, duration_hours=4,
                      earnings_terms={**TERMS, "short_visit_min_percent": 20},
                      actual_start_time=datetime.utcnow() - timedelta(hours=1))
    assert complete(client, ok, cg).status_code == 200
    assert txs(db, cg, type="earning")[0].status == "pending_clearance"
    cg2 = make_caregiver(db)
    short = make_booking(db, make_patient(db), cg2, status=BookingStatus.IN_PROGRESS, duration_hours=4,
                         earnings_terms={**TERMS, "short_visit_min_percent": 80},
                         actual_start_time=datetime.utcnow() - timedelta(hours=1))
    assert complete(client, short, cg2).status_code == 200
    tx = txs(db, cg2, type="earning")[0]
    assert tx.status == "held" and "under 80%" in tx.status_reason


def test_a_snapshot_from_before_the_field_existed_means_50(db):
    old = {"visit_share_percent": 80, "hold_hours": 24, "late_fee_share_percent": 80}
    b = timed(db, 59, earnings_terms=old)
    t = earnings.terms_for(db, b)
    assert t.short_visit_min_percent == 50 and earnings.is_short_visit(b, t.short_visit_min_percent)
    set_policy(db, short_visit_min_percent=5)                      # the live policy is not consulted for an old snapshot
    assert earnings.terms_for(db, b).short_visit_min_percent == 50


def test_the_admin_can_read_and_change_the_threshold_with_an_audit_row_and_it_is_validated(db, client):
    h = auth_headers(make_admin(db, ["super_admin"], password=None))
    url = "/api/v1/admin/booking-policy"
    assert client.get(url, headers=h).json()["short_visit_min_percent"] == 50
    r = client.patch(url, json={"reason": "Pilot: short visits are rare", "short_visit_min_percent": 40}, headers=h)
    assert r.status_code == 200 and r.json()["short_visit_min_percent"] == 40
    from app.db.models.audit import AdminAuditLog
    row = db.query(AdminAuditLog).filter_by(action="booking_policy.update").one()
    assert row.before["short_visit_min_percent"] == 50 and row.after["short_visit_min_percent"] == 40
    for bad in (101, -1, 40.5, "40"):
        assert client.patch(url, json={"reason": "x", "short_visit_min_percent": bad}, headers=h).status_code in (400, 422)


# === (d) the family's reason on a cancel ==================================================================================
def unpaid(db, patient, **kw):
    kw.setdefault("payment_expires_at", datetime.utcnow() + timedelta(minutes=20))
    kw.setdefault("payment_status", PaymentStatus.PENDING)
    return make_booking(db, patient, **kw)


def cancel(client, user, booking, **body):
    return client.post(f"{API}/{booking.id}/cancel", json={"accepted_fee_kobo": 0, **body}, headers=auth_headers(user))


def test_a_free_unpaid_cancel_stores_the_reason(db, client, fake_paystack):
    patient = make_patient(db)
    b = unpaid(db, patient)
    r = cancel(client, patient.user, b, reason="  Plans changed  ")
    assert r.status_code == 200, r.text
    fresh = reload(db, b)
    assert fresh.status == BookingStatus.CANCELLED and fresh.cancel_reason == "user_cancelled"
    assert fresh.cancel_note == "Plans changed"


def test_no_reason_blank_reason_and_long_reason(db, client, fake_paystack):
    patient = make_patient(db)
    none, blank, long_ = unpaid(db, patient), unpaid(db, patient), unpaid(db, patient)
    assert cancel(client, patient.user, none).status_code == 200
    assert cancel(client, patient.user, blank, reason="   ").status_code == 200
    assert cancel(client, patient.user, long_, reason="x" * 500).status_code == 200
    assert reload(db, none).cancel_note is None and reload(db, blank).cancel_note is None
    assert reload(db, long_).cancel_note == "x" * 500
    assert cancel(client, patient.user, unpaid(db, patient), reason="x" * 501).status_code == 422


def test_a_repeat_cancel_does_not_overwrite_the_first_reason(db, client, fake_paystack):
    patient = make_patient(db)
    b = unpaid(db, patient)
    assert cancel(client, patient.user, b, reason="first").status_code == 200
    again = cancel(client, patient.user, b, reason="second")
    assert again.status_code == 200 and again.json()["already_cancelled"] is True
    assert reload(db, b).cancel_note == "first"


def test_a_paid_cancel_stores_the_reason_too_and_keeps_it_on_the_refund_note(db, client, fake_paystack):
    from app.db.models.refund import Refund
    patient = make_patient(db)
    start = datetime.utcnow() + timedelta(days=3)
    b = make_booking(db, patient, scheduled_date=start, scheduled_end_date=start + timedelta(hours=4))
    r = cancel(client, patient.user, b, reason="Travelling")
    assert r.status_code == 200, r.text
    assert reload(db, b).cancel_note == "Travelling"
    assert "Travelling" in db.query(Refund).filter_by(booking_id=b.id).one().note


def test_the_family_s_reason_is_shown_to_no_caregiver_and_not_in_the_booking_views(db, client, fake_paystack):
    cg = make_caregiver(db)
    patient = make_patient(db)
    b = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED, cancel_note="Private family reason PLACEHOLDER",
                     earnings_terms=TERMS)
    for r in (client.get(f"{API}/booking/{b.id}", headers=auth_headers(cg.user)),
              client.get(f"{API}/caregiver/bookings", headers=auth_headers(cg.user)),
              act(client, b, cg, "arrived"),
              client.get(f"{API}/booking/{b.id}", headers=auth_headers(patient.user))):
        assert "PLACEHOLDER" not in r.text and "cancel_note" not in r.text
    assert "cancel_note" not in enrich_booking_with_details(b, db)
    assert "PLACEHOLDER" not in json.dumps(enrich_booking_with_details(b, db), default=str)
