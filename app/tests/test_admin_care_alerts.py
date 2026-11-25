"""The admin care-alert queue (slice 1c): list, detail, acknowledge, resolve, masking, audit, errors.

Roles used: Super Admin (everything), Clinical Reviewer (support.inbox + clinical.read, no people.pii), Operations (support.inbox +
people.pii, no clinical.read), Support (support.inbox only), Finance (none of them)."""

import json
import uuid
from datetime import datetime, timedelta

import pytest

from app.core.security import decrypt
from app.db.models.audit import AdminAuditLog
from app.db.models.care_alert import CareAlert
from app.tests.care_alert_helpers import ADMIN_ALERTS, outbox, patient_with_family, post_reading  # noqa: F401
from app.tests.factories import auth_headers, make_admin, make_patient
from app.tests.test_care_alerts_missed_tasks import VISIT_END_UTC, finished_visit, one_visit, run  # noqa: F401


@pytest.fixture
def admins(db):
    return {role: make_admin(db, [role], password=None, email=f"{role}@example.test")
            for role in ("super_admin", "clinical_reviewer", "operations", "support", "finance")}


def hdr(admins, role):
    return auth_headers(admins[role])


def make_alerts(db, client, outbox):
    """A critical heart-rate alert for Ada, a warning for Bola, and a missed-task alert."""
    ada = make_patient(db, full_name="Ada Okafor", phone_number="+2348012345678")
    bola = make_patient(db, full_name="Bola Adeyemi")
    post_reading(client, bola, "temperature", "38.5", "°C")
    post_reading(client, ada, "heart_rate", "45", "bpm")
    return ada, bola


def test_list_orders_critical_first_and_counts(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    r = client.get(ADMIN_ALERTS, headers=hdr(admins, "super_admin"))
    assert r.status_code == 200
    body = r.json()
    assert [(i["severity"], i["status"]) for i in body["items"]] == [("critical", "open"), ("warning", "open")]
    assert body["total"] == 2
    assert body["counts"] == {"open": 2, "acknowledged": 0, "resolved": 0, "critical_active": 1}
    first = body["items"][0]
    assert first["type"] == "vital_out_of_range" and first["summary"] == "Heart rate reading is at a critical level."
    assert first["patient"] == {"full_name": "Ada Okafor", "phone": "+2348012345678", "masked": False}
    assert first["detail"]["status"] == "critical" and first["detail"]["value"] == "45" and first["detail_masked"] is False
    assert first["acknowledged_by"] is None and first["resolved_by"] is None
    assert r.headers["cache-control"] == "no-store"


def test_masking_follows_people_pii_and_clinical_read(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    ops = client.get(ADMIN_ALERTS, headers=hdr(admins, "operations")).json()["items"][0]  # people.pii, no clinical.read
    assert ops["patient"]["full_name"] == "Ada Okafor" and ops["patient"]["masked"] is False
    assert ops["detail"] is None and ops["detail_masked"] is True and ops["note"] is None
    assert ops["summary"] == "Heart rate reading is at a critical level."  # the generic line is always there
    support = client.get(ADMIN_ALERTS, headers=hdr(admins, "support")).json()["items"][0]  # neither
    assert support["patient"]["full_name"] == "Ada O." and support["patient"]["masked"] is True
    assert support["patient"]["phone"] != "+2348012345678"
    clinical = client.get(ADMIN_ALERTS, headers=hdr(admins, "clinical_reviewer")).json()["items"][0]  # clinical.read, no people.pii
    assert clinical["patient"]["masked"] is True and clinical["detail"]["reason"]


def test_filters_status_severity_and_type(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    h = hdr(admins, "super_admin")
    assert client.get(f"{ADMIN_ALERTS}?severity=warning", headers=h).json()["total"] == 1
    assert client.get(f"{ADMIN_ALERTS}?type=task_missed", headers=h).json()["total"] == 0
    assert client.get(f"{ADMIN_ALERTS}?status=resolved", headers=h).json()["total"] == 0
    assert client.get(f"{ADMIN_ALERTS}?status=all", headers=h).json()["total"] == 2
    assert client.get(f"{ADMIN_ALERTS}?status=nonsense", headers=h).status_code == 422
    assert len(client.get(f"{ADMIN_ALERTS}?limit=1&offset=1", headers=h).json()["items"]) == 1


def test_acknowledge_then_resolve_with_notes_and_audit(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    h = hdr(admins, "super_admin")
    alert_id = client.get(ADMIN_ALERTS, headers=h).json()["items"][0]["id"]

    r = client.post(f"{ADMIN_ALERTS}/{alert_id}/acknowledge", json={"note": "Phoned the family, ambulance called"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "acknowledged" and body["acknowledged_by"]["email"] == "super_admin@example.test"
    assert body["acknowledged_at"] and body["note"] == "Phoned the family, ambulance called"
    assert client.get(ADMIN_ALERTS, headers=h).json()["counts"] == {"open": 1, "acknowledged": 1, "resolved": 0, "critical_active": 1}

    r = client.post(f"{ADMIN_ALERTS}/{alert_id}/resolve", json={"note": "Patient is at hospital"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved" and body["resolved_by"]["email"] == "super_admin@example.test"
    assert body["acknowledged_by"] is not None  # the acknowledgement stays on the record
    assert body["note"] == "Patient is at hospital"
    assert client.get(ADMIN_ALERTS, headers=h).json()["counts"]["critical_active"] == 0

    db.expire_all()
    stored = db.get(CareAlert, uuid.UUID(alert_id))
    assert "hospital" not in (stored.note_encrypted or "")  # encrypted at rest
    assert decrypt(stored.note_encrypted) == "Patient is at hospital"
    rows = db.query(AdminAuditLog).filter(AdminAuditLog.entity_type == "care_alert").order_by(AdminAuditLog.created_at).all()
    assert [r.action for r in rows] == ["care_alert.acknowledge", "care_alert.resolve"]
    assert rows[0].before == {"status": "open"}
    assert rows[0].after == {"status": "acknowledged", "severity": "critical", "type": "vital_out_of_range", "note_present": True}
    assert rows[1].before == {"status": "acknowledged"}
    assert "ambulance" not in json.dumps([r.after for r in rows]) and rows[0].reason is None  # the note never reaches the audit log


def test_an_alert_can_be_resolved_without_being_acknowledged(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    h = hdr(admins, "support")
    alert_id = client.get(ADMIN_ALERTS, headers=h).json()["items"][1]["id"]
    r = client.post(f"{ADMIN_ALERTS}/{alert_id}/resolve", json={}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "resolved" and r.json()["acknowledged_by"] is None
    row = db.query(AdminAuditLog).filter(AdminAuditLog.action == "care_alert.resolve").one()
    assert row.before == {"status": "open"} and row.after["note_present"] is False


def test_repeating_a_transition_is_a_409_with_a_code(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    h = hdr(admins, "super_admin")
    alert_id = client.get(ADMIN_ALERTS, headers=h).json()["items"][0]["id"]
    assert client.post(f"{ADMIN_ALERTS}/{alert_id}/acknowledge", json={}, headers=h).status_code == 200
    r = client.post(f"{ADMIN_ALERTS}/{alert_id}/acknowledge", json={}, headers=h)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "already_acknowledged", "message": "This alert has already been acknowledged."}
    client.post(f"{ADMIN_ALERTS}/{alert_id}/resolve", json={}, headers=h)
    for action in ("acknowledge", "resolve"):
        r = client.post(f"{ADMIN_ALERTS}/{alert_id}/{action}", json={}, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_resolved"
    assert db.query(AdminAuditLog).filter(AdminAuditLog.entity_type == "care_alert").count() == 2  # refusals leave no audit row


def test_an_unknown_alert_is_a_404_and_a_long_note_a_422(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    h = hdr(admins, "super_admin")
    missing = uuid.uuid4()
    for method, path in (("get", ""), ("post", "/acknowledge"), ("post", "/resolve")):
        kwargs = {"json": {}} if method == "post" else {}
        r = getattr(client, method)(f"{ADMIN_ALERTS}/{missing}{path}", headers=h, **kwargs)
        assert r.status_code == 404 and r.json()["detail"] == "Care alert not found"
    alert_id = client.get(ADMIN_ALERTS, headers=h).json()["items"][0]["id"]
    r = client.post(f"{ADMIN_ALERTS}/{alert_id}/resolve", json={"note": "x" * 1001}, headers=h)
    assert r.status_code == 422 and "x" * 20 not in r.text  # the rejected value is never echoed


def test_opening_the_detail_is_audited_only_when_the_clinical_detail_is_shown(db, client, outbox, admins):
    make_alerts(db, client, outbox)
    alert_id = client.get(ADMIN_ALERTS, headers=hdr(admins, "super_admin")).json()["items"][0]["id"]
    assert client.get(f"{ADMIN_ALERTS}/{alert_id}", headers=hdr(admins, "operations")).status_code == 200
    assert db.query(AdminAuditLog).filter(AdminAuditLog.action == "care_alert.read").count() == 0
    r = client.get(f"{ADMIN_ALERTS}/{alert_id}", headers=hdr(admins, "clinical_reviewer"))
    assert r.status_code == 200 and r.json()["detail"]["status"] == "critical"
    [row] = db.query(AdminAuditLog).filter(AdminAuditLog.action == "care_alert.read").all()
    assert row.entity_id == alert_id and row.after == {"alert_id": alert_id}


def test_finance_has_no_access_and_nobody_without_a_token(db, client, outbox, admins):
    assert client.get(ADMIN_ALERTS).status_code == 401
    r = client.get(ADMIN_ALERTS, headers=hdr(admins, "finance"))
    assert r.status_code == 403 and "support.inbox" in r.json()["detail"]


def test_a_missed_task_alert_lists_the_visit_and_the_titles(db, client, outbox, admins):
    one_visit(db)
    run(db)
    item = client.get(ADMIN_ALERTS, headers=hdr(admins, "super_admin")).json()["items"][0]
    assert item["type"] == "task_missed" and item["booking_code"] and item["booking_id"]
    assert item["detail"] == {"vital_type": None, "status": None, "reason": None, "value": None, "unit": None,
                              "tasks": ["Placeholder task one"], "days": ["2026-11-24"]}
    assert client.get(ADMIN_ALERTS, headers=hdr(admins, "support")).json()["items"][0]["detail"] is None


def test_the_family_delivery_state_is_visible_to_the_admin(db, client, outbox, admins):
    patient, _r, _family = patient_with_family(db)
    post_reading(client, patient, "heart_rate", "110", "bpm")
    item = client.get(ADMIN_ALERTS, headers=hdr(admins, "super_admin")).json()["items"][0]
    assert item["family_notified_count"] == 1 and item["family_notified_at"] is not None and item["admin_notified_at"] is None
