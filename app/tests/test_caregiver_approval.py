"""E5: caregiver approval API, mandatory background check, admin audit log."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models.audit import AdminAuditLog
from app.db.models.booking import BookingNotification, BookingStatus
from app.db.models.caregiver import Caregiver, CaregiverDocument, CaregiverSkill
from app.services.audit import AuditService
from app.tests.factories import (
    auth_headers,
    make_admin,
    make_booking,
    make_caregiver,
    make_patient,
    refresh_headers,
)

BASE = "/api/v1/admin/caregivers"
RANDOM = uuid.uuid4()


# --- helpers -------------------------------------------------------------------


def make_doc(db, caregiver, document_type="license", status="pending", **overrides):
    fields = dict(
        caregiver_id=caregiver.id,
        document_type=document_type,
        document_name="licence.pdf",
        file_path=f"caregivers/{caregiver.id}/documents/{uuid.uuid4()}.pdf",
        file_size=10,
        mime_type="application/pdf",
        verification_status=status,
    )
    fields.update(overrides)
    doc = CaregiverDocument(**fields)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def make_ready(db, **overrides):
    """A caregiver that satisfies every approve precondition."""
    fields = dict(
        terms_accepted=True,
        privacy_policy_accepted=True,
        background_check_consent=True,
        identity_verification_status="verified",
        background_check_status="cleared",
    )
    fields.update(overrides)
    cg = make_caregiver(db, approved=False, **fields)
    make_doc(db, cg, "license", "verified")
    db.add(CaregiverSkill(caregiver_id=cg.id, service_type="nurse", sub_specialty=None))
    db.commit()
    return cg


@pytest.fixture
def admin(db):
    return make_admin(db)


@pytest.fixture
def emails(monkeypatch):
    """Capture caregiver decision emails; nothing is sent."""
    captured = []
    import app.api.v1.endpoints.admin_caregivers as ep

    monkeypatch.setattr(ep, "send_caregiver_decision_email", lambda **kw: captured.append(kw))
    return captured


def audit_rows(db, action=None):
    db.expire_all()
    rows = db.execute(select(AdminAuditLog).order_by(AdminAuditLog.created_at)).scalars().all()
    return [r for r in rows if action is None or r.action == action]


def unmet_codes(response):
    return {u["code"] for u in response.json()["detail"]["unmet"]}


# --- auth: every endpoint -------------------------------------------------------

ENDPOINTS = [
    ("GET", BASE, None),
    ("GET", f"{BASE}/{RANDOM}", None),
    ("POST", f"{BASE}/documents/{RANDOM}/decision", {"decision": "verified"}),
    ("POST", f"{BASE}/{RANDOM}/identity-decision", {"decision": "verified", "notes": "ok"}),
    ("POST", f"{BASE}/{RANDOM}/background-check-decision", {"decision": "cleared", "notes": "ok"}),
    ("PUT", f"{BASE}/{RANDOM}/skills", {"skills": [{"service_type": "nurse"}]}),
    ("POST", f"{BASE}/{RANDOM}/approve", None),
    ("POST", f"{BASE}/{RANDOM}/reject", {"reason": "no"}),
    ("POST", f"{BASE}/{RANDOM}/request-changes", {"items": ["license"], "note": "n"}),
    ("POST", f"{BASE}/{RANDOM}/suspend", {"reason": "no"}),
    ("POST", f"{BASE}/{RANDOM}/reinstate", None),
]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_requires_token(client, method, path, body):
    assert client.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_refresh_token_is_rejected(db, client, method, path, body):
    user = make_admin(db)
    assert client.request(method, path, json=body, headers=refresh_headers(user)).status_code == 401


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
@pytest.mark.parametrize("kind", ["patient", "caregiver"])
def test_non_admin_is_forbidden(db, client, method, path, body, kind):
    user = make_patient(db).user if kind == "patient" else make_caregiver(db).user
    assert client.request(method, path, json=body, headers=auth_headers(user)).status_code == 403
    assert audit_rows(db) == []


# --- list / detail -------------------------------------------------------------


def test_list_defaults_to_under_review_oldest_first(db, client, admin):
    newer = make_caregiver(db, approved=False, consent_date=datetime.utcnow())
    older = make_caregiver(db, approved=False, consent_date=datetime.utcnow() - timedelta(days=5))
    make_caregiver(db, approved=True)  # approved: not in the default queue
    make_doc(db, older)
    make_doc(db, older, "id_document")

    r = client.get(BASE, headers=auth_headers(admin))
    assert r.status_code == 200
    body = r.json()
    assert [i["id"] for i in body["items"]] == [str(older.id), str(newer.id)]
    assert body["total"] == 2
    first = body["items"][0]
    assert first["document_count"] == 2
    assert first["age_in_days"] == 5
    assert first["profile_status"] == "under_review"
    for key in ("full_name", "state", "specialization", "years_of_experience",
                "identity_verification_status", "background_check_status", "submitted_at"):
        assert key in first


def test_list_filters_by_status_and_paginates(db, client, admin):
    for _ in range(3):
        make_caregiver(db, approved=True)
    r = client.get(f"{BASE}?profile_status=approved&limit=2&offset=0", headers=auth_headers(admin))
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2 and r.json()["total"] == 3
    r = client.get(f"{BASE}?profile_status=approved&limit=2&offset=2", headers=auth_headers(admin))
    assert len(r.json()["items"]) == 1
    assert client.get(f"{BASE}?profile_status=bogus", headers=auth_headers(admin)).status_code == 422
    assert client.get(f"{BASE}?limit=1000", headers=auth_headers(admin)).status_code == 422


def test_detail_has_everything_and_is_audited(db, client, admin):
    cg = make_ready(db)
    doc = db.query(CaregiverDocument).filter_by(caregiver_id=cg.id).one()
    r = client.get(f"{BASE}/{cg.id}", headers=auth_headers(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["id"] == str(cg.id)
    assert body["consents"]["terms_accepted"] is True
    assert body["documents"][0]["id"] == str(doc.id)
    assert body["documents"][0]["download_url"] == f"/api/v1/caregiver/documents/{doc.id}/file"
    assert body["identity"]["status"] == "verified"
    assert body["background_check"]["status"] == "cleared"
    assert body["skills"] == [{"service_type": "nurse", "sub_specialty": None}]
    assert isinstance(body["audit_history"], list)
    assert len(audit_rows(db, "caregiver.view")) == 1


def test_detail_404(client, admin):
    assert client.get(f"{BASE}/{uuid.uuid4()}", headers=auth_headers(admin)).status_code == 404


# --- document decision ---------------------------------------------------------


def test_document_verified_sets_fields_and_audits(db, client, admin):
    cg = make_caregiver(db, approved=False)
    doc = make_doc(db, cg)
    r = client.post(
        f"{BASE}/documents/{doc.id}/decision",
        json={"decision": "verified", "expires_on": "2030-01-31"},
        headers=auth_headers(admin),
    )
    assert r.status_code == 200
    db.refresh(doc)
    assert doc.verification_status == "verified"
    assert doc.verified_at is not None
    assert doc.reviewed_by == admin.id
    assert str(doc.expires_on) == "2030-01-31"
    (row,) = audit_rows(db, "caregiver_document.verified")
    assert row.admin_user_id == admin.id
    assert row.entity_type == "caregiver_document" and row.entity_id == str(doc.id)
    assert row.before["verification_status"] == "pending"
    assert row.after["verification_status"] == "verified"


def test_document_rejected_needs_reason(db, client, admin):
    cg = make_caregiver(db, approved=False)
    doc = make_doc(db, cg)
    url = f"{BASE}/documents/{doc.id}/decision"
    assert client.post(url, json={"decision": "rejected"}, headers=auth_headers(admin)).status_code == 422
    assert client.post(url, json={"decision": "approved"}, headers=auth_headers(admin)).status_code == 422
    r = client.post(url, json={"decision": "rejected", "reason": "Blurry"}, headers=auth_headers(admin))
    assert r.status_code == 200
    db.refresh(doc)
    assert (doc.verification_status, doc.rejection_reason, doc.verified_at) == ("rejected", "Blurry", None)
    (row,) = audit_rows(db, "caregiver_document.rejected")
    assert row.reason == "Blurry"


def test_document_decision_404(client, admin):
    r = client.post(f"{BASE}/documents/{uuid.uuid4()}/decision", json={"decision": "verified"},
                    headers=auth_headers(admin))
    assert r.status_code == 404


# --- identity / background decisions -------------------------------------------


def test_identity_decision(db, client, admin):
    cg = make_caregiver(db, approved=False)
    url = f"{BASE}/{cg.id}/identity-decision"
    assert client.post(url, json={"decision": "verified"}, headers=auth_headers(admin)).status_code == 422
    assert client.post(url, json={"decision": "verified", "notes": "  "},
                       headers=auth_headers(admin)).status_code == 422
    assert client.post(url, json={"decision": "cleared", "notes": "x"},
                       headers=auth_headers(admin)).status_code == 422
    r = client.post(url, json={"decision": "verified", "notes": "Matched NIN slip"}, headers=auth_headers(admin))
    assert r.status_code == 200
    db.refresh(cg)
    assert cg.identity_verification_status == "verified"
    (row,) = audit_rows(db, "caregiver.identity_decision")
    assert row.before["identity_verification_status"] == "pending"
    assert row.after["identity_verification_status"] == "verified"
    assert row.reason == "Matched NIN slip"


def test_background_check_decision_records_evidence(db, client, admin):
    cg = make_caregiver(db, approved=False)
    doc = make_doc(db, cg, "background_check")
    r = client.post(
        f"{BASE}/{cg.id}/background-check-decision",
        json={"decision": "cleared", "notes": "No record", "provider_reference": "REF-1",
              "document_id": str(doc.id)},
        headers=auth_headers(admin),
    )
    assert r.status_code == 200
    db.refresh(cg)
    assert cg.background_check_status == "cleared"
    assert cg.background_check_decided_by == admin.id
    assert cg.background_check_decided_at is not None
    assert cg.background_check_evidence == {
        "notes": "No record", "provider_reference": "REF-1", "document_id": str(doc.id)
    }
    (row,) = audit_rows(db, "caregiver.background_check_decision")
    assert row.before["background_check_status"] == "pending"
    assert row.after["background_check_status"] == "cleared"


def test_background_check_decision_validation(db, client, admin):
    cg = make_caregiver(db, approved=False)
    other = make_caregiver(db, approved=False)
    foreign_doc = make_doc(db, other)
    url = f"{BASE}/{cg.id}/background-check-decision"
    h = auth_headers(admin)
    assert client.post(url, json={"decision": "cleared"}, headers=h).status_code == 422  # notes
    assert client.post(url, json={"decision": "verified", "notes": "x"}, headers=h).status_code == 422
    r = client.post(url, json={"decision": "failed", "notes": "x", "document_id": str(foreign_doc.id)}, headers=h)
    assert r.status_code == 422  # a document that belongs to someone else
    db.refresh(cg)
    assert cg.background_check_status == "pending"
    assert audit_rows(db) == []


# --- skills ---------------------------------------------------------------------


def test_put_skills_replaces_the_full_list(db, client, admin):
    cg = make_caregiver(db, approved=False)
    url = f"{BASE}/{cg.id}/skills"
    h = auth_headers(admin)
    r = client.put(url, json={"skills": [
        {"service_type": "nurse", "sub_specialty": "Elderly Care"},
        {"service_type": "caregiver"},
    ]}, headers=h)
    assert r.status_code == 200
    r = client.put(url, json={"skills": [{"service_type": "physiotherapist"}]}, headers=h)
    assert r.status_code == 200
    skills = db.query(CaregiverSkill).filter_by(caregiver_id=cg.id).all()
    assert [(s.service_type, s.sub_specialty, s.created_by_admin_id) for s in skills] == [
        ("physiotherapist", None, admin.id)
    ]
    first, second = audit_rows(db, "caregiver.skills_set")
    assert len(first.after["skills"]) == 2 and first.before["skills"] == []
    assert len(second.before["skills"]) == 2 and len(second.after["skills"]) == 1


def test_put_skills_validation(db, client, admin):
    cg = make_caregiver(db, approved=False)
    url = f"{BASE}/{cg.id}/skills"
    h = auth_headers(admin)
    assert client.put(url, json={"skills": [{"service_type": "wizard"}]}, headers=h).status_code == 422
    # duplicates are rejected, including two entries with no sub_specialty
    dup = {"skills": [{"service_type": "nurse"}, {"service_type": "nurse"}]}
    assert client.put(url, json=dup, headers=h).status_code == 422
    assert client.put(f"{BASE}/{uuid.uuid4()}/skills", json={"skills": []}, headers=h).status_code == 404


def test_skills_unique_per_caregiver_service_and_sub_specialty(db):
    cg = make_caregiver(db, approved=False)
    db.add(CaregiverSkill(caregiver_id=cg.id, service_type="nurse"))
    db.commit()
    db.add(CaregiverSkill(caregiver_id=cg.id, service_type="nurse"))  # NULL sub_specialty twice
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# --- approve --------------------------------------------------------------------


def test_approve_success(db, client, admin, emails):
    cg = make_ready(db, changes_requested={"items": ["license"], "note": "old"}, rejection_reason="old")
    r = client.post(f"{BASE}/{cg.id}/approve", headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    db.refresh(cg)
    assert cg.profile_status == "approved"
    assert cg.approved_by == admin.id and cg.approved_at is not None
    assert cg.changes_requested is None and cg.rejection_reason is None
    (row,) = audit_rows(db, "caregiver.approve")
    assert row.before["profile_status"] == "under_review"
    assert row.after["profile_status"] == "approved"
    assert row.entity_id == str(cg.id)
    assert [e["decision"] for e in emails] == ["approved"]
    assert emails[0]["email_to"] == cg.email


def test_approve_with_everything_missing_lists_every_unmet(db, client, admin, emails):
    cg = make_caregiver(db, approved=False)  # no consents, no docs, no skills, nothing verified
    r = client.post(f"{BASE}/{cg.id}/approve", headers=auth_headers(admin))
    assert r.status_code == 409
    assert unmet_codes(r) == {
        "LICENSE_NOT_VERIFIED", "IDENTITY_NOT_VERIFIED", "BACKGROUND_CHECK_NOT_CLEARED",
        "NO_SKILLS", "CONSENTS_NOT_ACCEPTED",
    }
    assert all(u["message"] for u in r.json()["detail"]["unmet"])
    db.refresh(cg)
    assert cg.profile_status == "under_review"
    assert audit_rows(db) == [] and emails == []


def _delete_docs(db, cg):
    db.query(CaregiverDocument).filter_by(caregiver_id=cg.id).delete()


def _reject_docs(db, cg):
    db.query(CaregiverDocument).filter_by(caregiver_id=cg.id).update({"verification_status": "rejected"})


def _delete_skills(db, cg):
    db.query(CaregiverSkill).filter_by(caregiver_id=cg.id).delete()


@pytest.mark.parametrize(
    "break_it,code",
    [
        (_delete_docs, "LICENSE_NOT_VERIFIED"),
        (_reject_docs, "LICENSE_NOT_VERIFIED"),
        (lambda db, cg: make_doc(db, cg, "license", "pending"), "LICENSE_PENDING"),
        (lambda db, cg: setattr(cg, "identity_verification_status", "in_progress"), "IDENTITY_NOT_VERIFIED"),
        (lambda db, cg: setattr(cg, "identity_verification_status", "failed"), "IDENTITY_NOT_VERIFIED"),
        (lambda db, cg: setattr(cg, "background_check_status", "in_progress"), "BACKGROUND_CHECK_NOT_CLEARED"),
        (lambda db, cg: setattr(cg, "background_check_status", "failed"), "BACKGROUND_CHECK_NOT_CLEARED"),
        (_delete_skills, "NO_SKILLS"),
        (lambda db, cg: setattr(cg, "terms_accepted", False), "CONSENTS_NOT_ACCEPTED"),
        (lambda db, cg: setattr(cg, "privacy_policy_accepted", False), "CONSENTS_NOT_ACCEPTED"),
        (lambda db, cg: setattr(cg, "background_check_consent", False), "CONSENTS_NOT_ACCEPTED"),
        (lambda db, cg: setattr(cg, "profile_status", "incomplete"), "INVALID_STATE"),
        (lambda db, cg: setattr(cg, "profile_status", "suspended"), "INVALID_STATE"),
        (lambda db, cg: setattr(cg, "profile_status", "rejected"), "INVALID_STATE"),
    ],
)
def test_each_unmet_precondition_blocks_approval_and_is_named(db, client, admin, emails, break_it, code):
    cg = make_ready(db)
    break_it(db, cg)
    db.commit()
    before_status = db.get(Caregiver, cg.id).profile_status
    r = client.post(f"{BASE}/{cg.id}/approve", headers=auth_headers(admin))
    assert r.status_code == 409, r.text
    assert unmet_codes(r) == {code}
    db.expire_all()
    assert db.get(Caregiver, cg.id).profile_status == before_status
    assert audit_rows(db) == [] and emails == []


def test_a_pending_non_license_document_does_not_block_approval(db, client, admin):
    cg = make_ready(db)
    make_doc(db, cg, "id_document", "pending")
    assert client.post(f"{BASE}/{cg.id}/approve", headers=auth_headers(admin)).status_code == 200


def test_approve_404(client, admin):
    assert client.post(f"{BASE}/{uuid.uuid4()}/approve", headers=auth_headers(admin)).status_code == 404


def test_email_failure_does_not_undo_the_decision(db, client, admin, monkeypatch):
    import app.api.v1.endpoints.admin_caregivers as ep

    def boom(**kw):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(ep, "send_caregiver_decision_email", boom)
    cg = make_ready(db)
    r = client.post(f"{BASE}/{cg.id}/approve", headers=auth_headers(admin))
    assert r.status_code == 200
    db.refresh(cg)
    assert cg.profile_status == "approved"


# --- reject / request changes ---------------------------------------------------


def test_reject(db, client, admin, emails):
    cg = make_ready(db, changes_requested={"items": ["x"], "note": "n"})
    url = f"{BASE}/{cg.id}/reject"
    assert client.post(url, json={}, headers=auth_headers(admin)).status_code == 422
    assert client.post(url, json={"reason": " "}, headers=auth_headers(admin)).status_code == 422
    r = client.post(url, json={"reason": "Licence expired"}, headers=auth_headers(admin))
    assert r.status_code == 200
    db.refresh(cg)
    assert (cg.profile_status, cg.rejection_reason, cg.changes_requested) == ("rejected", "Licence expired", None)
    (row,) = audit_rows(db, "caregiver.reject")
    assert row.reason == "Licence expired"
    assert row.before["profile_status"] == "under_review" and row.after["profile_status"] == "rejected"
    assert emails[0]["decision"] == "rejected" and emails[0]["reason"] == "Licence expired"


def test_reject_conflicts_when_not_reviewable(db, client, admin, emails):
    for status in ("approved", "incomplete", "suspended", "rejected"):
        cg = make_caregiver(db, approved=False, profile_status=status)
        r = client.post(f"{BASE}/{cg.id}/reject", json={"reason": "x"}, headers=auth_headers(admin))
        assert r.status_code == 409, status
    assert audit_rows(db) == [] and emails == []


def test_request_changes(db, client, admin, emails):
    cg = make_ready(db)
    url = f"{BASE}/{cg.id}/request-changes"
    assert client.post(url, json={"items": [], "note": "n"}, headers=auth_headers(admin)).status_code == 422
    assert client.post(url, json={"items": ["license"], "note": ""}, headers=auth_headers(admin)).status_code == 422
    r = client.post(url, json={"items": ["license", "profile_photo"], "note": "Please re-upload"},
                    headers=auth_headers(admin))
    assert r.status_code == 200
    db.refresh(cg)
    assert cg.profile_status == "changes_requested"
    assert cg.changes_requested == {"items": ["license", "profile_photo"], "note": "Please re-upload"}
    (row,) = audit_rows(db, "caregiver.request_changes")
    assert row.after["changes_requested"]["items"] == ["license", "profile_photo"]
    assert emails[0]["decision"] == "changes_requested"
    assert emails[0]["items"] == ["license", "profile_photo"] and emails[0]["note"] == "Please re-upload"


def test_request_changes_only_from_under_review(db, client, admin):
    cg = make_caregiver(db, approved=True)
    r = client.post(f"{BASE}/{cg.id}/request-changes", json={"items": ["a"], "note": "n"},
                    headers=auth_headers(admin))
    assert r.status_code == 409


# --- suspend / reinstate --------------------------------------------------------


def test_suspend_and_reinstate(db, client, admin, emails):
    cg = make_ready(db, profile_status="approved")
    patient = make_patient(db)
    future = make_booking(db, patient, cg, status=BookingStatus.ACCEPTED)
    make_booking(db, patient, cg, status=BookingStatus.COMPLETED)  # not upcoming: not flagged
    h = auth_headers(admin)

    assert client.post(f"{BASE}/{cg.id}/suspend", json={}, headers=h).status_code == 422
    r = client.post(f"{BASE}/{cg.id}/suspend", json={"reason": "Complaint #12"}, headers=h)
    assert r.status_code == 200
    assert r.json()["affected_bookings"] == [str(future.id)]
    db.refresh(cg)
    assert cg.profile_status == "suspended"
    assert cg.suspension_reason == "Complaint #12" and cg.suspended_at is not None
    (row,) = audit_rows(db, "caregiver.suspend")
    assert row.before["profile_status"] == "approved" and row.after["profile_status"] == "suspended"
    assert row.after["affected_bookings"] == [str(future.id)]
    assert emails[-1]["decision"] == "suspended" and emails[-1]["reason"] == "Complaint #12"

    r = client.post(f"{BASE}/{cg.id}/reinstate", headers=h)
    assert r.status_code == 200
    db.refresh(cg)
    assert cg.profile_status == "approved"
    assert cg.suspended_at is None and cg.suspension_reason is None
    (row,) = audit_rows(db, "caregiver.reinstate")
    assert row.before["profile_status"] == "suspended" and row.after["profile_status"] == "approved"
    assert row.before["suspension_reason"] == "Complaint #12"


def test_suspend_only_approved_and_reinstate_only_suspended(db, client, admin):
    h = auth_headers(admin)
    under_review = make_caregiver(db, approved=False)
    approved = make_caregiver(db, approved=True)
    assert client.post(f"{BASE}/{under_review.id}/suspend", json={"reason": "x"}, headers=h).status_code == 409
    assert client.post(f"{BASE}/{approved.id}/reinstate", headers=h).status_code == 409
    assert audit_rows(db) == []


# --- matching -------------------------------------------------------------------


def test_suspended_caregiver_is_excluded_from_matching(db, client, admin):
    from app.db.models.booking import CaregiverLocation
    from app.services.location import LocationService

    cg = make_ready(db, profile_status="approved")
    db.add(CaregiverLocation(caregiver_id=cg.id, latitude=6.45, longitude=3.47,
                             is_active=True, is_available=True))
    db.commit()
    assert len(LocationService(db).find_nearby_caregivers(6.45, 3.47)) == 1

    assert client.post(f"{BASE}/{cg.id}/suspend", json={"reason": "x"},
                       headers=auth_headers(admin)).status_code == 200
    db.expire_all()
    assert LocationService(db).find_nearby_caregivers(6.45, 3.47) == []

    assert client.post(f"{BASE}/{cg.id}/reinstate", headers=auth_headers(admin)).status_code == 200
    db.expire_all()
    assert len(LocationService(db).find_nearby_caregivers(6.45, 3.47)) == 1


def test_suspended_caregiver_cannot_accept_an_earlier_offer(db, client):
    cg = make_caregiver(db, approved=True, profile_status="suspended")
    patient = make_patient(db)
    booking = make_booking(db, patient, None)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=cg.id, title="t", message="m",
                                   notification_type="new_booking"))
    db.commit()
    r = client.post(f"/api/v1/booking/caregiver/action/{booking.id}", json={"action": "accept"},
                    headers=auth_headers(cg.user))
    assert r.status_code == 403
    db.refresh(booking)
    assert booking.caregiver_id is None


# --- the caregiver cannot set any of these fields -------------------------------

PROTECTED = {
    "profile_status": "approved",
    "background_check_status": "cleared",
    "identity_verification_status": "verified",
    "approved_by": str(uuid.uuid4()),
    "approved_at": "2026-01-01T00:00:00",
    "suspended_at": "2026-01-01T00:00:00",
    "suspension_reason": "x",
    "rejection_reason": "x",
    "changes_requested": {"items": [], "note": ""},
    "background_check_evidence": {"notes": "self"},
    "background_check_decided_by": str(uuid.uuid4()),
    "background_check_decided_at": "2026-01-01T00:00:00",
}


def _snapshot(db, cg):
    db.expire_all()
    cg = db.get(Caregiver, cg.id)
    return {k: getattr(cg, k) for k in PROTECTED}


def test_caregiver_cannot_self_set_protected_fields(db, client):
    cg = make_caregiver(db, approved=False)
    before = _snapshot(db, cg)
    h = auth_headers(cg.user)
    step1 = {"date_of_birth": "1990-01-01", "gender": "female", "state": "Lagos", "address": "1 Rd",
             "emergency_contact_phone": "+2348000000000"}
    step2 = {"specialization": "nurse", "years_of_experience": 3, "preferred_work_hours": "{}",
             "service_areas": ["Lagos"]}
    consent = {"terms_accepted": False, "privacy_policy_accepted": False, "background_check_consent": False}
    assert client.put("/api/v1/caregiver/profile/step1", json={**step1, **PROTECTED}, headers=h).status_code == 200
    assert client.put("/api/v1/caregiver/profile/step2", json={**step2, **PROTECTED}, headers=h).status_code == 200
    assert client.put("/api/v1/caregiver/consent", json={**consent, **PROTECTED}, headers=h).status_code == 200
    assert _snapshot(db, cg) == before


def test_caregiver_document_upload_cannot_set_review_fields(db, client, tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path))
    cg = make_caregiver(db, approved=False)
    r = client.post(
        "/api/v1/caregiver/documents/upload",
        files={"file": ("l.pdf", b"%PDF-1.4 x", "application/pdf")},
        data={"document_type": "license", "verification_status": "verified",
              "reviewed_by": str(uuid.uuid4()), "rejection_reason": "x", "expires_on": "2030-01-01"},
        headers=auth_headers(cg.user),
    )
    assert r.status_code == 200
    doc = db.query(CaregiverDocument).filter_by(caregiver_id=cg.id).one()
    assert (doc.verification_status, doc.reviewed_by, doc.rejection_reason, doc.expires_on) == (
        "pending", None, None, None)


@pytest.mark.parametrize("status", ["approved", "suspended", "rejected", "changes_requested"])
def test_resubmitting_consent_cannot_escape_or_demote_a_decision(db, client, status):
    cg = make_caregiver(db, approved=False, profile_status=status, background_check_status="cleared",
                        terms_accepted=True, privacy_policy_accepted=True, background_check_consent=True)
    r = client.put(
        "/api/v1/caregiver/consent",
        json={"terms_accepted": True, "privacy_policy_accepted": True, "background_check_consent": True},
        headers=auth_headers(cg.user),
    )
    assert r.status_code == 200
    db.refresh(cg)
    expected = "under_review" if status == "changes_requested" else status
    assert cg.profile_status == expected
    assert cg.background_check_status == "cleared"


def test_first_consent_still_moves_incomplete_to_under_review(db, client):
    cg = make_caregiver(db, approved=False, profile_status="incomplete", background_check_status="pending")
    client.put("/api/v1/caregiver/consent",
               json={"terms_accepted": True, "privacy_policy_accepted": True, "background_check_consent": True},
               headers=auth_headers(cg.user))
    db.refresh(cg)
    assert (cg.profile_status, cg.background_check_status) == ("under_review", "in_progress")


def test_verification_status_exposes_decision_details(db, client):
    cg = make_caregiver(
        db, approved=False, profile_status="changes_requested", background_check_status="failed",
        rejection_reason="r", changes_requested={"items": ["license"], "note": "fix"},
        background_check_evidence={"notes": "internal"}, suspension_reason="internal",
    )
    r = client.get("/api/v1/caregiver/verification-status", headers=auth_headers(cg.user))
    assert r.status_code == 200
    body = r.json()
    assert body["profile_status"] == "changes_requested"
    assert body["background_check_status"] == "failed"
    assert body["rejection_reason"] == "r"
    assert body["changes_requested"] == {"items": ["license"], "note": "fix"}
    # internal evidence never reaches the caregiver
    assert "background_check_evidence" not in body and "suspension_reason" not in body


# --- form options ---------------------------------------------------------------


def test_form_options_states_are_nigerian(client):
    states = client.get("/api/v1/caregiver/form-options").json()["states"]
    assert len(states) == 37 and len(set(states)) == 37
    assert "Lagos" in states and "Federal Capital Territory" in states
    assert "Texas" not in states


# --- audit service --------------------------------------------------------------


def test_audit_record_captures_request_metadata(db, client, admin):
    cg = make_ready(db)
    client.post(f"{BASE}/{cg.id}/approve", headers={**auth_headers(admin), "User-Agent": "console/1.0"})
    (row,) = audit_rows(db, "caregiver.approve")
    assert row.user_agent == "console/1.0"
    assert row.ip  # TestClient peer
    assert row.created_at is not None


def test_audit_redacts_sensitive_keys(db, admin):
    AuditService(db).record(
        admin_user_id=admin.id, action="x.y", entity_type="x", entity_id="1",
        before={"password": "p", "ok": 1}, after={"nested": {"token": "t", "ok": 2}},
    )
    db.commit()
    (row,) = audit_rows(db, "x.y")
    assert row.before == {"password": "[redacted]", "ok": 1}
    assert row.after == {"nested": {"token": "[redacted]", "ok": 2}}


def test_audit_log_is_append_only_in_the_orm(db, admin):
    AuditService(db).record(admin_user_id=admin.id, action="x.y", entity_type="x", entity_id="1")
    db.commit()
    row = audit_rows(db)[0]
    row.reason = "tamper"
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()
    db.delete(db.query(AdminAuditLog).first())
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()


# --- concurrency ----------------------------------------------------------------


def test_concurrent_approve_and_reject_have_exactly_one_winner(db, client, admin):
    h = auth_headers(admin)
    for _ in range(3):
        cg = make_ready(db)
        cg_id = cg.id

        def approve():
            return client.post(f"{BASE}/{cg_id}/approve", headers=h).status_code

        def reject():
            return client.post(f"{BASE}/{cg_id}/reject", json={"reason": "x"}, headers=h).status_code

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(approve)
            b = pool.submit(reject)
            codes = sorted([a.result(), b.result()])
        assert codes == [200, 409]
        db.expire_all()
        assert db.get(Caregiver, cg_id).profile_status in ("approved", "rejected")
        rows = [r for r in audit_rows(db) if r.entity_id == str(cg_id)]
        assert len(rows) == 1
