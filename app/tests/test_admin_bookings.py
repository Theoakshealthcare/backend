"""A2: admin bookings operations: list, detail, unmatched queue and the actions (AD-FR-19..26)."""

import threading
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.security import encrypt
from app.db.models.audit import AdminAuditLog
from app.db.models.booking import (
    Booking,
    BookingDispatchWave,
    BookingNotification,
    BookingStatus,
    PaymentStatus,
)
from app.db.models.caregiver import Caregiver
from app.db.models.push_token import PushToken
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.db.models.admin import AdminUser
from app.schemas.admin_booking import AssignIn
from app.services.admin_bookings import AdminBookingService, Conflict
from app.services.booking import BookingService, BookingStateConflict
from app.schemas.booking import BookingActionRequest
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.test_dispatch import LAT, LON, make_candidate

BASE = "/api/v1/admin/bookings"
ACTION = "/api/v1/booking/caregiver/action"


def now() -> datetime:
    return datetime.utcnow()


def admin_headers(db, role="operations"):
    return auth_headers(make_admin(db, [role], password=None))


@pytest.fixture
def ops(db):
    return admin_headers(db, "operations")


@pytest.fixture
def boss(db):
    return admin_headers(db, "super_admin")


def candidate(db, i=0, **kw):
    return make_candidate(db, i, at=now(), **kw)


def paid(db, patient=None, **kw):
    kw.setdefault("payment_status", PaymentStatus.PAID)
    return make_booking(db, patient or make_patient(db), **kw)


def fresh(db, booking):
    db.expire_all()
    return db.get(Booking, booking.id)


def audit_rows(db, booking, action=None):
    db.expire_all()
    q = db.query(AdminAuditLog).filter(AdminAuditLog.entity_type == "booking",
                                       AdminAuditLog.entity_id == str(booking.id))
    if action:
        q = q.filter(AdminAuditLog.action == action)
    return q.order_by(AdminAuditLog.created_at).all()


def post(client, headers, booking, path, body):
    return client.post(f"{BASE}/{booking.id}/{path}", json=body, headers=headers)


# === list ====================================================================================================


def test_list_filters(client, db, ops):
    cg = make_caregiver(db)
    a = paid(db, make_patient(db, full_name="Ada Okafor"), caregiver=cg, status=BookingStatus.ACCEPTED,
             channel="whatsapp", dispatch_state="matched")
    b = paid(db, make_patient(db, full_name="Bola Tinubu"), patient_city="Abuja", dispatch_state="unmatched")
    c = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, service_type="doctor")

    def ids(**params):
        r = client.get(BASE, params=params, headers=ops)
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()["items"]}

    assert ids() == {str(a.id), str(b.id), str(c.id)}
    assert ids(status="accepted") == {str(a.id)}
    assert ids(payment_status="pending") == {str(c.id)}
    assert ids(dispatch_state="unmatched") == {str(b.id)}
    assert ids(channel="whatsapp") == {str(a.id)}
    assert ids(city="abuja") == {str(b.id)}
    assert ids(service_type="doctor") == {str(c.id)}
    assert ids(caregiver_id=str(cg.id)) == {str(a.id)}
    assert ids(q=a.booking_code.lower()) == {str(a.id)}
    assert ids(q="okafor") == {str(a.id)}
    assert ids(q=cg.full_name) == {str(a.id)}
    assert ids(q="100%") == set()  # LIKE wildcards are escaped, not interpreted


def test_list_searches_by_phone_in_any_format(client, db, ops):
    patient = make_patient(db, phone_number="+2348031234567")
    b = paid(db, patient)
    paid(db)
    for q in ("+2348031234567", "08031234567", "2348031234567", "1234567"):
        r = client.get(BASE, params={"q": q}, headers=ops)
        assert [i["id"] for i in r.json()["items"]] == [str(b.id)], q


def test_list_finds_a_whatsapp_patient_by_the_phone_on_the_user(client, db, ops):
    patient = make_patient(db, phone_number=None)
    patient.user.phone_number = "+2348099990000"
    db.commit()
    b = paid(db, patient, channel="whatsapp")
    r = client.get(BASE, params={"q": "08099990000"}, headers=ops)
    assert [i["id"] for i in r.json()["items"]] == [str(b.id)]
    assert r.json()["items"][0]["patient"]["phone"] == "+2348099990000"


def test_list_date_ranges_sort_and_pagination(client, db, ops):
    base = now() + timedelta(days=10)
    rows = [paid(db, scheduled_date=base + timedelta(days=i), scheduled_end_date=base + timedelta(days=i, hours=4),
                 total_amount_kobo=1_000_000 * (i + 1)) for i in range(5)]
    r = client.get(BASE, params={"scheduled_from": (base + timedelta(days=1)).isoformat(),
                                 "scheduled_to": (base + timedelta(days=4)).isoformat(), "order": "asc"},
                   headers=ops)
    assert [i["id"] for i in r.json()["items"]] == [str(x.id) for x in rows[1:4]]
    r = client.get(BASE, params={"sort": "total_amount_kobo", "order": "desc", "limit": 2, "offset": 1}, headers=ops)
    body = r.json()
    assert body["total"] == 5 and body["limit"] == 2 and body["offset"] == 1
    assert [i["total_amount_kobo"] for i in body["items"]] == [4_000_000, 3_000_000]
    assert client.get(BASE, params={"created_from": (now() + timedelta(days=1)).isoformat()},
                      headers=ops).json()["total"] == 0


def test_list_validates_its_parameters(client, ops):
    assert client.get(BASE, params={"status": "nonsense"}, headers=ops).status_code == 422
    assert client.get(BASE, params={"limit": 1000}, headers=ops).status_code == 422
    assert client.get(BASE, params={"sort": "booking_code"}, headers=ops).status_code == 422


def test_list_masks_the_patient_without_people_pii(client, db):
    paid(db, make_patient(db, full_name="Ada Okafor", phone_number="+2348031234567"))
    ops_item = client.get(BASE, headers=admin_headers(db, "operations")).json()["items"][0]
    fin_item = client.get(BASE, headers=admin_headers(db, "finance")).json()["items"][0]
    assert ops_item["patient"] == {**ops_item["patient"], "full_name": "Ada Okafor", "phone": "+2348031234567",
                                   "masked": False}
    assert fin_item["patient"]["full_name"] == "Ada O."
    assert fin_item["patient"]["phone"] == "+234****4567"
    assert fin_item["patient"]["masked"] is True


def test_list_flags_suspected_and_flagged_no_shows(client, db, ops):
    cg = make_caregiver(db)
    late = paid(db, caregiver=cg, status=BookingStatus.ACCEPTED,
                scheduled_date=now() - timedelta(minutes=settings.NO_SHOW_GRACE_MINUTES + 1))
    on_time = paid(db, caregiver=cg, status=BookingStatus.ACCEPTED, scheduled_date=now() + timedelta(hours=2))
    started = paid(db, caregiver=cg, status=BookingStatus.IN_PROGRESS, actual_start_time=now(),
                   scheduled_date=now() - timedelta(hours=1))
    r = client.get(BASE, params={"no_show": True}, headers=ops).json()["items"]
    assert [i["id"] for i in r] == [str(late.id)] and r[0]["no_show_suspected"] is True
    r = client.get(BASE, params={"no_show": False}, headers=ops).json()["items"]
    assert {i["id"] for i in r} == {str(on_time.id), str(started.id)}


# === detail ==================================================================================================


def test_detail_has_every_section_and_a_timeline(client, db, boss):
    cg = candidate(db)
    b = paid(db, status=BookingStatus.PENDING, channel="whatsapp", condition="Placeholder condition")
    from app.services.dispatch import start_dispatch
    start_dispatch(db, b.id, now())
    db.add(BookingEvent(booking_id=b.id, event="payment_received", created_at=now(), delivered_at=now()))
    db.commit()
    assert post(client, boss, b, "cancel", {"reason_code": "duplicate", "reason": "dupe", "refund": "full"}
                ).status_code == 200

    r = client.get(f"{BASE}/{b.id}", headers=boss)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["booking_code"] == b.booking_code and d["status"] == "cancelled" and d["channel"] == "whatsapp"
    assert d["price"]["total_kobo"] == b.total_amount_kobo
    assert d["payment"]["status"] == "paid" and d["payment"]["paystack_reference"] == b.paystack_reference
    assert d["payment"]["refund_required"] is True
    assert [w["wave_no"] for w in d["dispatch"]["waves"]] == [1]
    assert d["dispatch"]["waves"][0]["notified_count"] == 1
    assert d["dispatch"]["offers"][0]["caregiver_id"] == str(cg.id)
    assert {e["event"] for e in d["events"]} == {"payment_received", "booking_cancelled_by_admin"}
    kinds = [t["kind"] for t in d["timeline"]]
    assert kinds[0] == "booking_created" and "wave_sent" in kinds and "admin_action" in kinds
    assert [t["at"] for t in d["timeline"]] == sorted(t["at"] for t in d["timeline"])
    admin_entry = next(t for t in d["timeline"] if t["kind"] == "admin_action")
    assert admin_entry["title"] == "booking.cancel" and admin_entry["detail"] == "dupe"
    assert admin_entry["actor"].startswith("admin: ")
    assert client.get(f"{BASE}/{b.id}", headers=boss).status_code == 200
    assert client.get(f"{BASE}/00000000-0000-0000-0000-000000000000", headers=boss).status_code == 404


def test_address_is_only_shown_with_the_address_permission(client, db):
    b = paid(db, patient_address="12 Placeholder Street, Lekki")
    ops_d = client.get(f"{BASE}/{b.id}", headers=admin_headers(db, "operations")).json()
    fin_d = client.get(f"{BASE}/{b.id}", headers=admin_headers(db, "finance")).json()
    assert ops_d["address"]["visible"] is True and ops_d["address"]["line"] == "12 Placeholder Street, Lekki"
    assert ops_d["address"]["latitude"] == b.patient_latitude
    assert fin_d["address"] == {"city": "Lagos", "state": "Lagos", "visible": False, "line": None,
                                "latitude": None, "longitude": None, "needs_address_check": False}
    assert "Placeholder Street" not in str(fin_d)


def test_condition_is_masked_without_a_clinical_permission(client, db):
    b = paid(db, channel="whatsapp", condition_encrypted=encrypt("Secret-Condition-HIV"),
             special_instructions="Gate code 0000")
    for role in ("operations", "finance", "support"):
        r = client.get(f"{BASE}/{b.id}", headers=admin_headers(db, role))
        assert r.status_code == 200
        assert r.json()["clinical"] == {"masked": True, "has_condition": True, "has_special_instructions": True,
                                        "condition": None, "special_instructions": None}
        assert "Secret-Condition" not in r.text and "Gate code" not in r.text
    assert audit_rows(db, b, "booking.clinical_view") == []  # nothing was revealed, so nothing to audit


@pytest.mark.parametrize("field", ["condition_encrypted", "condition"])
def test_condition_is_decrypted_for_clinical_read_and_the_access_is_audited(client, db, field):
    value = encrypt("Secret-Condition-HIV") if field == "condition_encrypted" else "Secret-Condition-HIV"
    b = paid(db, special_instructions="Gate code 0000", **{field: value})
    clinical = admin_headers(db, "clinical_reviewer")
    r = client.get(f"{BASE}/{b.id}", headers=clinical)
    assert r.status_code == 200
    assert r.json()["clinical"] == {"masked": False, "has_condition": True, "has_special_instructions": True,
                                    "condition": "Secret-Condition-HIV", "special_instructions": "Gate code 0000"}
    rows = audit_rows(db, b, "booking.clinical_view")
    assert len(rows) == 1 and rows[0].after == {"fields": ["condition", "special_instructions"]}
    assert "Secret-Condition" not in str(rows[0].after)  # the audit row names the fields, never the content
    client.get(f"{BASE}/{b.id}", headers=clinical)
    assert len(audit_rows(db, b, "booking.clinical_view")) == 2  # every read is recorded


def test_available_actions_follow_state_and_permissions(client, db):
    held = paid(db, needs_address_check=True, channel="whatsapp")
    open_ = paid(db, dispatch_state="unmatched")
    cg = make_caregiver(db)
    visit = paid(db, caregiver=cg, status=BookingStatus.IN_PROGRESS)
    actions = lambda b, role: set(client.get(f"{BASE}/{b.id}", headers=admin_headers(db, role)).json()  # noqa: E731
                                  ["available_actions"])
    assert actions(held, "operations") == {"confirm_address", "cancel"}
    assert actions(open_, "operations") == {"redispatch", "assign", "cancel"}
    assert actions(open_, "super_admin") == {"redispatch", "assign", "force_assign", "cancel"}
    assert actions(visit, "operations") == set()
    assert actions(visit, "super_admin") == {"correct_status"}
    assert actions(open_, "finance") == set()


# === unmatched queue ==========================================================================================


def test_unmatched_queue_lists_the_three_reasons_soonest_first(client, db, ops):
    t = now() + timedelta(days=2)
    long_ago = now() - timedelta(minutes=settings.UNMATCHED_SLA_MINUTES + 5)
    held = paid(db, needs_address_check=True, channel="whatsapp", scheduled_date=t + timedelta(hours=3),
                scheduled_end_date=t + timedelta(hours=7))
    unmatched = paid(db, dispatch_state="unmatched", dispatch_started_at=long_ago, scheduled_date=t,
                     scheduled_end_date=t + timedelta(hours=4))
    breached = paid(db, dispatch_state="dispatching", dispatch_started_at=long_ago,
                    scheduled_date=t + timedelta(hours=1), scheduled_end_date=t + timedelta(hours=5))
    # none of these belong in the queue
    paid(db, dispatch_state="dispatching", dispatch_started_at=now())              # still inside the SLA
    paid(db, caregiver=make_caregiver(db), status=BookingStatus.ACCEPTED, dispatch_state="matched")
    paid(db, dispatch_state="unmatched", status=BookingStatus.CANCELLED)
    make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, dispatch_state="unmatched")

    body = client.get(f"{BASE}/unmatched", headers=ops).json()
    assert body["total"] == 3
    assert [(i["id"], i["queue_reason"]) for i in body["items"]] == [
        (str(unmatched.id), "unmatched"), (str(breached.id), "sla_breached"), (str(held.id), "address_check")]
    assert body["items"][0]["waiting_minutes"] >= settings.UNMATCHED_SLA_MINUTES + 5
    only = client.get(f"{BASE}/unmatched", params={"reason": "address_check"}, headers=ops).json()
    assert [i["id"] for i in only["items"]] == [str(held.id)]


def test_a_held_booking_is_not_double_counted_as_unmatched(client, db, ops):
    paid(db, needs_address_check=True, dispatch_state="unmatched")
    assert client.get(f"{BASE}/unmatched", params={"reason": "unmatched"}, headers=ops).json()["total"] == 0
    assert client.get(f"{BASE}/unmatched", headers=ops).json()["total"] == 1


# === re-dispatch ==============================================================================================


def test_redispatch_sends_a_new_wave_and_audits(client, db, ops, expo):
    cg = candidate(db)
    token = f"ExponentPushToken[{cg.id.hex[:8]}]"
    db.add(PushToken(user_id=cg.user_id, token=token, platform="ios"))
    db.commit()
    b = paid(db, dispatch_state="unmatched", dispatch_started_at=now() - timedelta(hours=1))

    r = post(client, ops, b, "redispatch", {"reason": "Nobody online earlier"})
    assert r.status_code == 200, r.text
    assert r.json()["dispatch_state"] == "dispatching" and r.json()["notified_caregivers"] == 1
    b = fresh(db, b)
    assert b.dispatch_state == "dispatching" and b.dispatch_started_at > now() - timedelta(minutes=1)
    assert [w.wave_no for w in db.query(BookingDispatchWave).filter_by(booking_id=b.id)] == [1]
    assert db.query(BookingNotification).filter_by(booking_id=b.id, caregiver_id=cg.id).count() == 1
    assert [m["to"] for m in expo.messages] == [token]
    assert "Lagos" in expo.messages[0]["body"] and b.patient_address not in str(expo.messages)
    (row,) = audit_rows(db, b, "booking.redispatch")
    assert row.reason == "Nobody online earlier" and row.before["dispatch_state"] == "unmatched"
    assert row.after["dispatch_state"] == "dispatching" and row.after["notified"] == 1


def test_redispatch_with_nobody_to_offer_stays_unmatched(client, db, ops):
    b = paid(db, dispatch_state="unmatched")
    r = post(client, ops, b, "redispatch", {"reason": "try again"})
    assert r.status_code == 200 and r.json()["dispatch_state"] == "unmatched" and r.json()["notified_caregivers"] == 0
    assert len(audit_rows(db, b, "booking.redispatch")) == 1  # the attempt is on record either way


def test_redispatch_can_widen_the_radius_and_reoffer_to_earlier_caregivers(client, db, ops):
    far = candidate(db, lat_offset=0.3)  # about 33 km away, outside the default radius
    b = paid(db, dispatch_state="unmatched")
    assert post(client, ops, b, "redispatch", {"reason": "default radius"}).json()["notified_caregivers"] == 0
    r = post(client, ops, b, "redispatch", {"reason": "wider", "radius_km": 50})
    assert r.json()["notified_caregivers"] == 1
    again = post(client, ops, b, "redispatch", {"reason": "again", "radius_km": 50})
    assert again.json()["notified_caregivers"] == 0  # already offered: skipped by default
    again = post(client, ops, b, "redispatch", {"reason": "again", "radius_km": 50,
                                                "include_previously_notified": True})
    assert again.json()["notified_caregivers"] == 1
    assert db.query(BookingNotification).filter_by(booking_id=b.id, caregiver_id=far.id).count() == 1


def test_redispatch_refuses_the_wrong_states(client, db, ops):
    cg = make_caregiver(db)
    for b in (paid(db, caregiver=cg, status=BookingStatus.ACCEPTED), paid(db, status=BookingStatus.CANCELLED),
              make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING),
              paid(db, needs_address_check=True)):
        assert post(client, ops, b, "redispatch", {"reason": "x"}).status_code == 409
        assert audit_rows(db, b) == []


# === assign / reassign ========================================================================================


def test_assign_offers_the_visit_and_the_caregiver_still_accepts(client, db, ops, expo):
    cg = candidate(db)
    db.add(PushToken(user_id=cg.user_id, token="ExponentPushToken[assign]", platform="ios"))
    db.commit()
    b = paid(db, dispatch_state="unmatched")

    r = post(client, ops, b, "assign", {"caregiver_id": str(cg.id), "reason": "Known to the patient"})
    assert r.status_code == 200, r.text
    b2 = fresh(db, b)
    assert b2.status == BookingStatus.PENDING and b2.caregiver_id is None  # not assigned until they accept
    assert [m["to"] for m in expo.messages] == ["ExponentPushToken[assign]"]
    (row,) = audit_rows(db, b, "booking.assign")
    assert row.reason == "Known to the patient" and row.after["target_caregiver_id"] == str(cg.id)

    accept = client.post(f"{ACTION}/{b.id}", json={"action": "accept"}, headers=auth_headers(cg.user))
    assert accept.status_code == 200
    b3 = fresh(db, b)
    assert b3.caregiver_id == cg.id and b3.status == BookingStatus.ACCEPTED and b3.dispatch_state == "matched"


def test_assign_refuses_an_unapproved_or_unknown_caregiver(client, db, ops):
    b = paid(db)
    for cg in (make_caregiver(db, approved=False), make_caregiver(db, profile_status="suspended")):
        r = post(client, ops, b, "assign", {"caregiver_id": str(cg.id), "reason": "x"})
        assert r.status_code == 409 and "not approved" in r.json()["detail"]
    r = post(client, ops, b, "assign", {"caregiver_id": "00000000-0000-0000-0000-000000000000", "reason": "x"})
    assert r.status_code == 404
    assert audit_rows(db, b) == [] and db.query(BookingNotification).count() == 0


def test_assign_refuses_a_held_or_unpaid_booking(client, db, ops):
    cg = make_caregiver(db)
    for b in (paid(db, needs_address_check=True),
              make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)):
        assert post(client, ops, b, "assign", {"caregiver_id": str(cg.id), "reason": "x"}).status_code == 409


def test_force_assign_is_super_admin_only_and_skips_the_accept(client, db, ops, boss):
    cg = candidate(db)
    other = candidate(db, 1)
    b = paid(db)
    start = {"caregiver_id": str(cg.id), "reason": "Patient is waiting", "force": True}
    r = post(client, ops, b, "assign", start)
    assert r.status_code == 403 and "Super Admin" in r.json()["detail"]
    assert fresh(db, b).caregiver_id is None and audit_rows(db, b) == []

    # an offer outstanding to someone else is closed, as when a caregiver accepts
    db.add(BookingNotification(booking_id=b.id, caregiver_id=other.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    r = post(client, boss, b, "assign", start)
    assert r.status_code == 200, r.text
    b2 = fresh(db, b)
    assert b2.caregiver_id == cg.id and b2.status == BookingStatus.ACCEPTED and b2.dispatch_state == "matched"
    assert b2.visit_code_enc is not None
    notes = {n.caregiver_id: n.response for n in db.query(BookingNotification).filter_by(booking_id=b.id)}
    assert notes == {cg.id: "assigned_by_admin", other.id: "auto_declined"}
    (row,) = audit_rows(db, b, "booking.force_assign")
    assert row.reason == "Patient is waiting"


def test_reassign_moves_an_accepted_booking_and_tells_both_caregivers(client, db, ops, expo):
    old, new = candidate(db), candidate(db, 1)
    for cg in (old, new):
        db.add(PushToken(user_id=cg.user_id, token=f"ExponentPushToken[{cg.id.hex[:8]}]", platform="ios"))
    db.commit()
    b = paid(db, caregiver=old, status=BookingStatus.EN_ROUTE, dispatch_state="matched",
             visit_code="1234", no_show_flagged_at=now())
    db.add(BookingNotification(booking_id=b.id, caregiver_id=old.id, title="t", message="m",
                               notification_type="new_booking", response="accept"))
    db.commit()

    assert post(client, ops, b, "assign", {"caregiver_id": str(new.id), "reason": "x"}).status_code == 409
    r = post(client, ops, b, "reassign", {"caregiver_id": str(new.id), "reason": "Caregiver no-show"})
    assert r.status_code == 200, r.text
    b2 = fresh(db, b)
    assert b2.caregiver_id == new.id and b2.status == BookingStatus.ACCEPTED and b2.no_show_flagged_at is None
    assert {m["to"] for m in expo.messages} == {f"ExponentPushToken[{c.id.hex[:8]}]" for c in (old, new)}
    titles = {n.caregiver_id: n.title for n in db.query(BookingNotification)
              .filter(BookingNotification.booking_id == b.id, BookingNotification.title.like("Visit %"))}
    assert titles == {old.id: "Visit reassigned", new.id: "Visit assigned"}
    old_offer = db.query(BookingNotification).filter_by(booking_id=b.id, caregiver_id=old.id,
                                                        notification_type="new_booking").one()
    assert old_offer.response == "reassigned"
    assert len(audit_rows(db, b, "booking.reassign")) == 1
    # the old caregiver can no longer act on it
    gone = client.post(f"{ACTION}/{b.id}", json={"action": "start", "visit_code": "0000"},
                       headers=auth_headers(old.user))
    assert gone.status_code == 403


def test_reassign_to_the_same_caregiver_or_a_started_visit_is_refused(client, db, ops):
    cg, other = make_caregiver(db), make_caregiver(db)
    b = paid(db, caregiver=cg, status=BookingStatus.ACCEPTED)
    assert post(client, ops, b, "reassign", {"caregiver_id": str(cg.id), "reason": "x"}).status_code == 409
    started = paid(db, caregiver=cg, status=BookingStatus.IN_PROGRESS)
    assert post(client, ops, started, "reassign", {"caregiver_id": str(other.id), "reason": "x"}).status_code == 409
    pending = paid(db)
    assert post(client, ops, pending, "reassign", {"caregiver_id": str(other.id), "reason": "x"}).status_code == 409
    assert audit_rows(db, b) == []


# === cancel ===================================================================================================


def cancel_body(**kw):
    return {"reason_code": "patient_request", "reason": "Patient called", "refund": "none", **kw}


def test_cancel_with_a_full_refund_flags_it_and_tells_the_booker(client, db, ops):
    b = paid(db, channel="whatsapp", dispatch_state="dispatching")
    r = post(client, ops, b, "cancel", cancel_body(refund="full", reason_code="no_supply"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled" and r.json()["refund_required"] is True
    b2 = fresh(db, b)
    assert b2.status == BookingStatus.CANCELLED and b2.cancel_reason == "no_supply"
    assert b2.refund_required and b2.refund_required_reason == "admin_cancelled_full"
    assert b2.payment_status == PaymentStatus.PAID  # no money moved: the refund queue does that
    assert b2.refund_amount_kobo == b2.total_amount_kobo
    assert sorted(e.event for e in db.query(BookingEvent).filter_by(booking_id=b.id)) == [
        "booking_cancelled_by_admin"]  # the booker is told it is cancelled; the refund is announced when it is sent
    (row,) = audit_rows(db, b, "booking.cancel")
    assert row.reason == "Patient called" and row.after["refund"] == "full"
    assert row.before["status"] == "pending" and row.after["status"] == "cancelled"


def test_cancel_with_a_partial_refund_records_the_amount(client, db, ops):
    b = paid(db, total_amount_kobo=4_400_000)
    assert post(client, ops, b, "cancel", cancel_body(refund="partial")).status_code == 422  # amount missing
    assert post(client, ops, b, "cancel", cancel_body(refund="none", refund_amount_kobo=5)).status_code == 422
    assert post(client, ops, b, "cancel", cancel_body(refund="partial", refund_amount_kobo=4_400_000)
                ).status_code == 422  # that is a full refund
    assert fresh(db, b).status == BookingStatus.PENDING
    r = post(client, ops, b, "cancel", cancel_body(refund="partial", refund_amount_kobo=1_000_000))
    assert r.status_code == 200 and r.json()["refund_required_reason"] == "admin_cancelled_partial"
    assert r.json()["refund_amount_kobo"] == 1_000_000 and fresh(db, b).refund_amount_kobo == 1_000_000


def test_cancel_without_a_refund_leaves_the_refund_queue_alone(client, db, ops):
    b = paid(db, channel="whatsapp")
    assert post(client, ops, b, "cancel", cancel_body(reason_code="duplicate")).status_code == 200
    b2 = fresh(db, b)
    assert b2.refund_required is False and b2.refund_required_reason is None and b2.refund_amount_kobo is None
    assert [e.event for e in db.query(BookingEvent)] == ["booking_cancelled_by_admin"]  # told it is cancelled, no refund


def test_cancel_an_unpaid_booking_offers_no_refund(client, db, ops):
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    assert post(client, ops, b, "cancel", cancel_body(refund="full")).status_code == 422
    assert post(client, ops, b, "cancel", cancel_body()).status_code == 200


def test_cancel_closes_open_offers_and_tells_the_assigned_caregiver(client, db, ops, expo):
    cg = make_caregiver(db)
    db.add(PushToken(user_id=cg.user_id, token="ExponentPushToken[cancel]", platform="ios"))
    offered = make_caregiver(db)
    b = paid(db, caregiver=cg, status=BookingStatus.ACCEPTED)
    db.add(BookingNotification(booking_id=b.id, caregiver_id=offered.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    assert post(client, ops, b, "cancel", cancel_body(reason_code="safety", refund="full")).status_code == 200
    db.expire_all()
    assert db.query(BookingNotification).filter_by(caregiver_id=offered.id).one().response == "booking_cancelled"
    assert db.query(BookingNotification).filter_by(caregiver_id=cg.id, title="Visit cancelled").count() == 1
    assert [m["to"] for m in expo.messages] == ["ExponentPushToken[cancel]"]
    assert expo.messages[0]["data"] == {"type": "job_cancelled", "booking_id": str(b.id)}


def test_cancel_validates_the_reason_code_and_state(client, db, ops):
    b = paid(db)
    assert post(client, ops, b, "cancel", cancel_body(reason_code="bored")).status_code == 422
    assert post(client, ops, b, "cancel", {"reason_code": "other", "refund": "none"}).status_code == 422
    assert post(client, ops, b, "cancel", cancel_body(reason="   ")).status_code == 422
    for status in (BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.IN_PROGRESS):
        done = paid(db, caregiver=make_caregiver(db), status=status)
        assert post(client, ops, done, "cancel", cancel_body()).status_code == 409
    assert audit_rows(db, b) == []


# === confirm address ==========================================================================================


def held_booking(db, **kw):
    return paid(db, needs_address_check=True, channel="whatsapp", patient_latitude=6.5244,
                patient_longitude=3.3792, **kw)


def test_confirm_address_clears_the_hold_and_starts_dispatch(client, db, ops):
    cg = candidate(db)
    b = held_booking(db)
    r = post(client, ops, b, "confirm-address", {"reason": "Called the patient", "latitude": LAT, "longitude": LON,
                                                 "address": "14 Real Close, Lekki"})
    assert r.status_code == 200, r.text
    assert r.json()["needs_address_check"] is False and r.json()["dispatch_state"] == "dispatching"
    assert r.json()["notified_caregivers"] == 1
    b2 = fresh(db, b)
    assert (b2.patient_latitude, b2.patient_longitude) == (LAT, LON)
    assert b2.patient_address == "14 Real Close, Lekki" and b2.dispatch_started_at is not None
    assert [w.wave_no for w in db.query(BookingDispatchWave).filter_by(booking_id=b.id)] == [1]
    assert db.query(BookingNotification).filter_by(booking_id=b.id, caregiver_id=cg.id).count() == 1
    (row,) = audit_rows(db, b, "booking.confirm_address")
    assert row.reason == "Called the patient"
    # the audit trail is readable by Finance (audit.read), so it never carries the patient's coordinates
    assert row.after["location_set"] is True and "latitude" not in row.before | row.after and "longitude" not in row.before | row.after
    assert str(LAT) not in str(row.before) + str(row.after)


def test_confirm_address_on_an_unpaid_booking_only_clears_the_hold(client, db, ops):
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING, needs_address_check=True)
    r = post(client, ops, b, "confirm-address", {"reason": "ok", "latitude": LAT, "longitude": LON})
    assert r.status_code == 200 and r.json()["dispatch_state"] is None
    assert db.query(BookingDispatchWave).count() == 0


def test_confirm_address_validates_and_refuses_the_wrong_state(client, db, ops):
    b = held_booking(db)
    ok = {"reason": "ok", "latitude": LAT, "longitude": LON}
    assert post(client, ops, b, "confirm-address", {**ok, "latitude": 51.5, "longitude": -0.12}).status_code == 422
    assert post(client, ops, b, "confirm-address", {**ok, "latitude": LON, "longitude": LAT}).status_code == 422
    assert post(client, ops, b, "confirm-address", {"latitude": LAT, "longitude": LON}).status_code == 422
    assert fresh(db, b).needs_address_check is True
    assert post(client, ops, paid(db), "confirm-address", ok).status_code == 409  # nothing to confirm
    assert post(client, ops, held_booking(db, status=BookingStatus.CANCELLED), "confirm-address", ok
                ).status_code == 409


def test_confirm_address_is_not_repeatable(client, db, ops):
    b = held_booking(db)
    ok = {"reason": "ok", "latitude": LAT, "longitude": LON}
    assert post(client, ops, b, "confirm-address", ok).status_code == 200
    assert post(client, ops, b, "confirm-address", ok).status_code == 409
    assert len(audit_rows(db, b, "booking.confirm_address")) == 1


# === no-show ==================================================================================================


def test_flag_no_show_marks_the_booking_and_audits(client, db, ops):
    cg = make_caregiver(db)
    b = paid(db, caregiver=cg, status=BookingStatus.ACCEPTED, scheduled_date=now() - timedelta(minutes=20))
    r = post(client, ops, b, "no-show", {"reason": "Not reachable"})
    assert r.status_code == 200 and r.json()["no_show_flagged_at"] is not None
    assert fresh(db, b).no_show_flagged_by is not None
    (row,) = audit_rows(db, b, "booking.flag_no_show")
    assert row.reason == "Not reachable" and row.after["caregiver_id"] == str(cg.id)
    assert post(client, ops, b, "no-show", {"reason": "again"}).status_code == 409


def test_flag_no_show_needs_a_due_unstarted_accepted_visit(client, db, ops):
    cg = make_caregiver(db)
    future = paid(db, caregiver=cg, status=BookingStatus.ACCEPTED)
    pending = paid(db, scheduled_date=now() - timedelta(hours=1))
    started = paid(db, caregiver=cg, status=BookingStatus.IN_PROGRESS, scheduled_date=now() - timedelta(hours=1),
                   actual_start_time=now())
    for b in (future, pending, started):
        assert post(client, ops, b, "no-show", {"reason": "x"}).status_code == 409


# === status correction ========================================================================================


def test_correct_status_completes_a_visit_with_the_normal_fields(client, db, boss):
    cg = make_caregiver(db)
    b = paid(db, caregiver=cg, status=BookingStatus.IN_PROGRESS, actual_start_time=now() - timedelta(hours=4))
    r = post(client, boss, b, "correct-status", {"status": "completed", "reason": "Caregiver forgot to check out"})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    b2 = fresh(db, b)
    assert b2.actual_end_time is not None and b2.actual_start_time < b2.actual_end_time
    (row,) = audit_rows(db, b, "booking.correct_status")
    assert row.before["status"] == "in_progress" and row.after["status"] == "completed"
    assert row.reason == "Caregiver forgot to check out"


def test_correct_status_from_accepted_fills_in_the_start_time(client, db, boss):
    b = paid(db, caregiver=make_caregiver(db), status=BookingStatus.ACCEPTED)
    assert post(client, boss, b, "correct-status", {"status": "completed", "reason": "x"}).status_code == 200
    b2 = fresh(db, b)
    assert b2.actual_start_time is not None and b2.actual_end_time is not None


def test_correct_status_refuses_impossible_moves(client, db, boss, ops):
    cg = make_caregiver(db)
    for status, target in ((BookingStatus.PENDING, "completed"), (BookingStatus.COMPLETED, "in_progress"),
                           (BookingStatus.CANCELLED, "completed"), (BookingStatus.IN_PROGRESS, "en_route")):
        b = paid(db, caregiver=cg if status != BookingStatus.PENDING else None, status=status)
        assert post(client, boss, b, "correct-status", {"status": target, "reason": "x"}).status_code == 409
        assert audit_rows(db, b) == []
    assert post(client, boss, paid(db), "correct-status", {"status": "cancelled", "reason": "x"}).status_code == 422
    b = paid(db, caregiver=cg, status=BookingStatus.IN_PROGRESS)
    assert post(client, ops, b, "correct-status", {"status": "completed", "reason": "x"}).status_code == 403


# === every action needs a reason ==============================================================================


@pytest.mark.parametrize("path,body", [
    ("redispatch", {}), ("assign", {"caregiver_id": "00000000-0000-0000-0000-000000000001"}),
    ("reassign", {"caregiver_id": "00000000-0000-0000-0000-000000000001"}),
    ("cancel", {"reason_code": "other", "refund": "none"}), ("no-show", {}),
    ("confirm-address", {"latitude": LAT, "longitude": LON}),
])
def test_every_action_requires_a_reason(client, db, boss, path, body):
    b = paid(db)
    r = post(client, boss, b, path, body)
    assert r.status_code == 422 and "reason" in r.text
    assert post(client, boss, b, path, {**body, "reason": "  "}).status_code == 422
    assert audit_rows(db, b) == []


def test_candidates_lists_matches_best_first(client, db, ops):
    near, far = candidate(db, 0), candidate(db, 8)
    b = paid(db)
    r = client.get(f"{BASE}/{b.id}/candidates", headers=ops)
    assert r.status_code == 200
    assert [c["caregiver_id"] for c in r.json()["items"]] == [str(near.id), str(far.id)]
    assert set(r.json()["items"][0]) == {"caregiver_id", "full_name", "specialization", "distance_km",
                                         "estimated_arrival_minutes", "match_score"}


# === concurrency ==============================================================================================


@pytest.mark.parametrize("round_", range(3))
def test_admin_force_assign_and_caregiver_accept_have_exactly_one_winner(db, round_):
    """Both take the booking row lock, so whichever comes second sees the booking already taken."""
    caregiver_a, caregiver_b = candidate(db, 0), candidate(db, 1)
    booking = paid(db)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver_a.id, title="t", message="m",
                               notification_type="new_booking"))
    admin = make_admin(db, ["super_admin"], password=None)
    db.commit()
    # plain ids: the ORM objects belong to this thread's session
    booking_id, admin_id, id_a, id_b = booking.id, admin.id, caregiver_a.id, caregiver_b.id
    barrier = threading.Barrier(2)
    outcome = {}

    def admin_assigns():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            service = AdminBookingService(session)
            service.assign(booking_id, session.get(AdminUser, admin_id),
                           AssignIn(caregiver_id=id_b, reason="force", force=True), None)
            outcome["admin"] = "won"
        except Conflict:
            outcome["admin"] = "lost"
        finally:
            session.close()

    def caregiver_accepts():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            BookingService(session).handle_booking_action(
                booking_id, id_a, BookingActionRequest(action="accept"))
            outcome["caregiver"] = "won"
        except BookingStateConflict:
            outcome["caregiver"] = "lost"
        finally:
            session.close()

    threads = [threading.Thread(target=admin_assigns), threading.Thread(target=caregiver_accepts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcome.values()) == ["lost", "won"], outcome
    final = fresh(db, booking)
    winner = id_b if outcome["admin"] == "won" else id_a
    assert final.caregiver_id == winner and final.status == BookingStatus.ACCEPTED
    assert db.query(Caregiver).count() == 2


def test_two_admins_cancelling_at_once_only_one_succeeds(db):
    booking = paid(db, channel="whatsapp")
    booking_id = booking.id
    admin_ids = [make_admin(db, ["operations"], password=None).id for _ in range(2)]
    barrier = threading.Barrier(2)
    results = []

    def cancel(admin_id):
        from app.schemas.admin_booking import CancelIn
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            AdminBookingService(session).cancel(
                booking_id, session.get(AdminUser, admin_id),
                CancelIn(reason_code="duplicate", reason="x", refund="full"), None)
            results.append("ok")
        except Conflict:
            results.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=cancel, args=(a,)) for a in admin_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["conflict", "ok"]
    assert len(audit_rows(db, booking, "booking.cancel")) == 1
    assert sorted(e.event for e in db.query(BookingEvent).filter_by(booking_id=booking.id)) == [
        "booking_cancelled_by_admin"]  # one cancel, so told exactly once


def test_search_by_name_or_phone_needs_people_pii(client, db):
    b = paid(db, make_patient(db, full_name="Ada Okafor", phone_number="+2348031234567"))
    fin = admin_headers(db, "finance")
    for q in ("okafor", "08031234567"):
        assert client.get(BASE, params={"q": q}, headers=fin).json()["total"] == 0
    assert client.get(BASE, params={"q": b.booking_code}, headers=fin).json()["total"] == 1


def test_admin_notices_never_appear_as_new_requests(client, db, ops):
    old, new = candidate(db), candidate(db, 1)
    b = paid(db, caregiver=old, status=BookingStatus.ACCEPTED)
    assert post(client, ops, b, "reassign", {"caregiver_id": str(new.id), "reason": "x"}).status_code == 200
    r = client.get("/api/v1/booking/caregiver/bookings", params={"status": "new_requests"},
                   headers=auth_headers(old.user))
    assert r.status_code == 200 and r.json() == []
    assert post(client, ops, b, "cancel", cancel_body()).status_code == 200
    r = client.get("/api/v1/booking/caregiver/bookings", params={"status": "new_requests"},
                   headers=auth_headers(new.user))
    assert r.status_code == 200 and r.json() == []
