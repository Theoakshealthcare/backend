"""Shape lock for endpoints that gained typed response models.

The golden file maps every key (recursively) to its JSON type. It was generated from the untyped
endpoints before the response models were added, so a model that drops, adds, renames or retypes a
field fails here. Regenerate deliberately with ``REGEN_SHAPES=1`` only when a shape change is intended
(and then update the FE guides).
"""

import json
import os
from datetime import date
from pathlib import Path

import pytest

from app.core.config import settings
from app.db.models.booking import BookingNotification, BookingStatus, PaymentStatus
from app.db.models.caregiver import CaregiverDocument, CaregiverSkill
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient

GOLDEN = Path(__file__).parent / "golden" / "response_shapes.json"
ACTION = "/api/v1/booking/caregiver/action"
ADMIN_CG = "/api/v1/admin/caregivers"


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def _offered(db, caregiver, **overrides):
    patient = make_patient(db)
    booking = make_booking(db, patient, status=BookingStatus.PENDING, payment_status=PaymentStatus.PAID, **overrides)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    return booking


@pytest.fixture
def captured(db, client, monkeypatch, kyc_availability):
    monkeypatch.setattr(settings, "DOJAH_WIDGET_ID", "placeholder-widget-id")
    monkeypatch.setattr(settings, "DOJAH_APP_ID", "placeholder-app-id")
    monkeypatch.setattr(settings, "DOJAH_PUBLIC_KEY", "placeholder-public-key")
    monkeypatch.setattr(settings, "KYC_WIDGET_PUBLIC_BASE_URL", "https://api.oakly.test")
    out = {}
    cg = make_caregiver(db, approved=False)
    out["verification_status"] = client.get("/api/v1/caregiver/verification-status", headers=auth_headers(cg.user))
    kyc = make_caregiver(db, approved=False, profile_completion_step=2, date_of_birth=date(1990, 4, 12))
    kyc_url = "/api/v1/caregiver/identity-verification"
    consent = {"consent": True, "policy_version": settings.KYC_POLICY_VERSION}
    client.post(f"{kyc_url}/sessions", json=consent, headers=auth_headers(kyc.user))  # 201; the shape pinned is the 200 re-read
    out["identity_session"] = client.post(f"{kyc_url}/sessions", json=consent, headers=auth_headers(kyc.user))
    out["identity_state"] = client.get(kyc_url, headers=auth_headers(kyc.user))
    out["form_options"] = client.get("/api/v1/caregiver/form-options")

    worker = make_caregiver(db)
    booking = _offered(db, worker)
    out["action_accept"] = client.post(f"{ACTION}/{booking.id}", json={"action": "accept"},
                                       headers=auth_headers(worker.user))
    other = make_caregiver(db)
    offered = _offered(db, other)
    out["action_decline"] = client.post(f"{ACTION}/{offered.id}", json={"action": "decline"},
                                        headers=auth_headers(other.user))

    admin = make_admin(db)
    reviewee = make_caregiver(db, approved=False, profile_status="under_review")
    db.add(CaregiverDocument(caregiver_id=reviewee.id, document_type="license", document_name="l.pdf",
                             file_path="x/l.pdf", file_size=10, mime_type="application/pdf",
                             verification_status="pending"))
    db.add(CaregiverSkill(caregiver_id=reviewee.id, service_type="nurse", sub_specialty=None))
    db.commit()
    headers = auth_headers(admin)
    out["admin_list"] = client.get(ADMIN_CG, headers=headers)
    client.get(f"{ADMIN_CG}/{reviewee.id}", headers=headers)  # first view leaves an audit row
    out["admin_detail"] = client.get(f"{ADMIN_CG}/{reviewee.id}", headers=headers)
    return out


def test_shapes_match_golden(captured):
    for name, r in captured.items():
        assert r.status_code == 200, (name, r.text)
    actual = {name: shape(r.json()) for name, r in captured.items()}
    if os.environ.get("REGEN_SHAPES"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert actual == golden


def test_action_money_keeps_its_string_format_and_dates_their_iso_format(captured):
    booking = captured["action_accept"].json()["booking"]
    assert booking["total_amount"] == f"{booking['total_amount_kobo'] / 100:.2f}"
    assert booking["service_fee"] == f"{booking['service_fee_kobo'] / 100:.2f}"
    assert "T" in booking["scheduled_date"] and not booking["scheduled_date"].endswith("Z")
    assert booking["status"] == "accepted" and booking["payment_status"] == "paid"


def test_typed_endpoints_are_in_the_openapi_document(client):
    schemas = client.get("/api/v1/openapi.json").json()["components"]["schemas"]
    for name in ("VerificationStatusOut", "FormOptionsOut", "CaregiverActionResponse",
                 "ApplicationList", "ApplicationDetail"):
        assert name in schemas, name
