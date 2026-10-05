"""E5 review fixes: lock races, auto-suspend on negative decisions, reinstate re-check."""

import time
from concurrent.futures import ThreadPoolExecutor

from app.db.models.booking import BookingNotification, BookingStatus
from app.db.models.caregiver import Caregiver, CaregiverDocument
from app.db.session import SessionLocal
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient
from app.tests.test_caregiver_approval import (  # noqa: F401  (fixtures + helpers)
    BASE,
    admin,
    audit_rows,
    emails,
    make_doc,
    make_ready,
    unmet_codes,
)


def _hold_caregiver_lock(cg_id, new_status=None):
    """Simulate another transaction mid-way through an admin action on this caregiver."""
    other = SessionLocal()
    row = other.query(Caregiver).filter(Caregiver.id == cg_id).with_for_update().one()
    if new_status:
        row.profile_status = new_status
    return other


def _blocked_then_released(fn, other):
    with ThreadPoolExecutor(1) as pool:
        fut = pool.submit(fn)
        time.sleep(0.7)
        assert not fut.done(), "the call did not wait for the lock"
        other.commit()
        other.close()
        return fut.result(timeout=10)


def test_accept_waits_for_an_in_flight_suspend_then_is_refused(db, client):
    cg = make_caregiver(db, approved=True)
    booking = make_booking(db, make_patient(db), None)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=cg.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    other = _hold_caregiver_lock(cg.id, "suspended")  # admin's suspend, not yet committed
    r = _blocked_then_released(
        lambda: client.post(f"/api/v1/booking/caregiver/action/{booking.id}", json={"action": "accept"},
                            headers=auth_headers(cg.user)),
        other,
    )
    assert r.status_code == 403
    db.refresh(booking)
    assert booking.caregiver_id is None


def test_upload_waits_for_the_caregiver_row_lock(db, client, tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path))
    cg = make_caregiver(db, approved=False)
    other = _hold_caregiver_lock(cg.id)  # e.g. an approve in progress
    r = _blocked_then_released(
        lambda: client.post("/api/v1/caregiver/documents/upload",
                            files={"file": ("l.pdf", b"%PDF-1.4 x", "application/pdf")},
                            data={"document_type": "license"}, headers=auth_headers(cg.user)),
        other,
    )
    assert r.status_code == 200


def _approved_with_booking(db):
    cg = make_ready(db, profile_status="approved")
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED)
    return cg, booking


def test_failed_identity_on_an_approved_caregiver_auto_suspends(db, client, admin, emails):
    cg, booking = _approved_with_booking(db)
    r = client.post(f"{BASE}/{cg.id}/identity-decision", json={"decision": "failed", "notes": "mismatch"},
                    headers=auth_headers(admin))
    assert r.status_code == 200
    assert r.json()["auto_suspended"] is True and r.json()["affected_bookings"] == [str(booking.id)]
    db.refresh(cg)
    assert cg.profile_status == "suspended"
    assert cg.suspension_reason == "identity failed after approval"
    assert len(audit_rows(db, "caregiver.identity_decision")) == 1
    (row,) = audit_rows(db, "caregiver.auto_suspend")
    assert row.before["profile_status"] == "approved" and row.after["profile_status"] == "suspended"
    assert row.after["affected_bookings"] == [str(booking.id)]
    assert emails[-1]["decision"] == "suspended"


def test_failed_background_check_on_an_approved_caregiver_auto_suspends(db, client, admin):
    cg, _ = _approved_with_booking(db)
    r = client.post(f"{BASE}/{cg.id}/background-check-decision", json={"decision": "failed", "notes": "record"},
                    headers=auth_headers(admin))
    assert r.status_code == 200 and r.json()["auto_suspended"] is True
    db.refresh(cg)
    assert (cg.profile_status, cg.suspension_reason) == ("suspended", "background check failed after approval")
    assert len(audit_rows(db, "caregiver.auto_suspend")) == 1


def test_rejecting_the_only_verified_licence_of_an_approved_caregiver_auto_suspends(db, client, admin):
    cg = make_ready(db, profile_status="approved")
    doc = db.query(CaregiverDocument).filter_by(caregiver_id=cg.id).one()
    r = client.post(f"{BASE}/documents/{doc.id}/decision", json={"decision": "rejected", "reason": "forged"},
                    headers=auth_headers(admin))
    assert r.status_code == 200 and r.json()["auto_suspended"] is True
    db.refresh(cg)
    assert (cg.profile_status, cg.suspension_reason) == ("suspended", "licence rejected after approval")
    assert len(audit_rows(db, "caregiver_document.rejected")) == 1
    assert len(audit_rows(db, "caregiver.auto_suspend")) == 1


def test_rejecting_a_spare_document_does_not_suspend(db, client, admin):
    cg = make_ready(db, profile_status="approved")
    second = make_doc(db, cg, "license", "verified")
    id_doc = make_doc(db, cg, "id_document", "verified")
    for d in (second, id_doc):
        r = client.post(f"{BASE}/documents/{d.id}/decision", json={"decision": "rejected", "reason": "x"},
                        headers=auth_headers(admin))
        assert r.status_code == 200 and r.json()["auto_suspended"] is False
    db.refresh(cg)
    assert cg.profile_status == "approved"
    assert audit_rows(db, "caregiver.auto_suspend") == []


def test_negative_decision_on_a_caregiver_not_yet_approved_does_not_suspend(db, client, admin):
    cg = make_ready(db)
    r = client.post(f"{BASE}/{cg.id}/identity-decision", json={"decision": "failed", "notes": "x"},
                    headers=auth_headers(admin))
    assert r.status_code == 200 and r.json()["auto_suspended"] is False
    db.refresh(cg)
    assert cg.profile_status == "under_review"


def test_reinstate_rechecks_the_approval_preconditions(db, client, admin):
    cg = make_ready(db, profile_status="approved")
    h = auth_headers(admin)
    client.post(f"{BASE}/{cg.id}/identity-decision", json={"decision": "failed", "notes": "x"}, headers=h)
    db.refresh(cg)
    assert cg.profile_status == "suspended"
    r = client.post(f"{BASE}/{cg.id}/reinstate", headers=h)
    assert r.status_code == 409
    assert unmet_codes(r) == {"IDENTITY_NOT_VERIFIED"}
    db.refresh(cg)
    assert cg.profile_status == "suspended"
    client.post(f"{BASE}/{cg.id}/identity-decision", json={"decision": "verified", "notes": "re-checked"},
                headers=h)
    assert client.post(f"{BASE}/{cg.id}/reinstate", headers=h).status_code == 200
