"""WP-C item 4: prescriptions require authentication and ownership."""

import uuid

import pytest

import app.api.v1.endpoints.prescription as prescription_endpoints
from app.db.models.prescription import Prescription
from app.db.models.prescription_settings import PrescriptionSettings
from app.tests.factories import (
    auth_headers,
    make_admin,
    make_caregiver,
    make_patient,
    refresh_headers,
)

BASE = "/api/v1/prescription"


@pytest.fixture
def settings_row(db):
    row = PrescriptionSettings(base_fee_kobo=500000, multipliers={"regular": 1.0}, discount=0)
    db.add(row)
    db.commit()
    return row


def make_prescription(db, patient) -> Prescription:
    rx = Prescription(
        patient_id=patient.id,
        requested_by_user_id=patient.user_id,
        medication_name="Paracetamol",
        delivery_option="regular",
        address="1 Test Close, Lekki",
        total_cost_kobo=500000,
    )
    db.add(rx)
    db.commit()
    db.refresh(rx)
    return rx


def create_body(patient, requested_by=None):
    return {
        "patient_id": str(patient.id),
        "requested_by_user_id": str(requested_by or patient.user_id),
        "medication_name": "Paracetamol",
        "delivery_option": "regular",
        "address": "1 Test Close, Lekki",
    }


# --- create -------------------------------------------------------------------


def test_create_requires_token(db, client, settings_row):
    patient = make_patient(db)
    assert client.post(f"{BASE}/create", json=create_body(patient)).status_code == 401


def test_create_rejects_refresh_token(db, client, settings_row):
    patient = make_patient(db)
    r = client.post(f"{BASE}/create", json=create_body(patient), headers=refresh_headers(patient.user))
    assert r.status_code == 401


def test_create_for_own_patient_succeeds_and_requester_is_the_caller(db, client, settings_row):
    patient = make_patient(db)
    spoofed = uuid.uuid4()
    r = client.post(
        f"{BASE}/create", json=create_body(patient, requested_by=spoofed), headers=auth_headers(patient.user)
    )
    assert r.status_code == 200, r.text
    assert r.json()["requested_by_user_id"] == str(patient.user_id)


def test_create_for_another_patient_is_forbidden(db, client, settings_row):
    me, other = make_patient(db), make_patient(db)
    r = client.post(f"{BASE}/create", json=create_body(other), headers=auth_headers(me.user))
    assert r.status_code == 403
    assert db.query(Prescription).count() == 0


def test_create_by_caregiver_is_forbidden(db, client, settings_row):
    patient = make_patient(db)
    caregiver = make_caregiver(db)
    r = client.post(f"{BASE}/create", json=create_body(patient), headers=auth_headers(caregiver.user))
    assert r.status_code == 403


# --- reads --------------------------------------------------------------------


def test_list_for_patient_requires_token(db, client):
    patient = make_patient(db)
    assert client.get(f"{BASE}/patient/{patient.id}").status_code == 401


def test_list_for_patient_owner_admin_and_stranger(db, client):
    owner, stranger = make_patient(db), make_patient(db)
    admin = make_admin(db)
    make_prescription(db, owner)
    url = f"{BASE}/patient/{owner.id}"

    r = client.get(url, headers=auth_headers(owner.user))
    assert r.status_code == 200 and len(r.json()) == 1
    assert client.get(url, headers=auth_headers(admin)).status_code == 200
    assert client.get(url, headers=auth_headers(stranger.user)).status_code == 403
    assert client.get(url, headers=refresh_headers(owner.user)).status_code == 401


def test_list_for_patient_forbidden_for_caregiver(db, client):
    owner = make_patient(db)
    caregiver = make_caregiver(db)
    r = client.get(f"{BASE}/patient/{owner.id}", headers=auth_headers(caregiver.user))
    assert r.status_code == 403


@pytest.mark.parametrize("suffix", ["", "/status"])
def test_get_one_requires_token_and_ownership(db, client, suffix):
    owner, stranger = make_patient(db), make_patient(db)
    admin = make_admin(db)
    rx = make_prescription(db, owner)
    url = f"{BASE}/{rx.id}{suffix}"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth_headers(owner.user)).status_code == 200
    assert client.get(url, headers=auth_headers(admin)).status_code == 200
    assert client.get(url, headers=auth_headers(stranger.user)).status_code == 403
    assert client.get(url, headers=refresh_headers(owner.user)).status_code == 401


def test_get_one_missing_is_404_for_authenticated_user(db, client):
    patient = make_patient(db)
    r = client.get(f"{BASE}/{uuid.uuid4()}", headers=auth_headers(patient.user))
    assert r.status_code == 404


# --- admin-only mutations ------------------------------------------------------


def test_patch_is_admin_only(db, client):
    owner = make_patient(db)
    admin = make_admin(db)
    rx = make_prescription(db, owner)
    url = f"{BASE}/{rx.id}"
    body = {"doctor_note_path": "notes/x.pdf"}

    assert client.patch(url, json=body).status_code == 401
    assert client.patch(url, json=body, headers=auth_headers(owner.user)).status_code == 401
    assert client.patch(url, json=body, headers=refresh_headers(admin)).status_code == 401
    db.refresh(rx)
    assert rx.doctor_note_path is None

    assert client.patch(url, json=body, headers=auth_headers(admin)).status_code == 200
    db.refresh(rx)
    assert rx.doctor_note_path == "notes/x.pdf"


@pytest.mark.parametrize(
    "body",
    [{"payment_status": "paid"}, {"total_cost": 1}, {"paystack_reference": "ref_x"}],
)
def test_patch_cannot_change_payment_state_even_for_admin(db, client, body):
    # payment state only changes through the payment webhook / settlement (forbidden -> 422)
    admin = make_admin(db)
    rx = make_prescription(db, make_patient(db))
    r = client.patch(f"{BASE}/{rx.id}", json=body, headers=auth_headers(admin))
    assert r.status_code == 422
    db.refresh(rx)
    assert (rx.payment_status, rx.total_cost_kobo, rx.paystack_reference) == ("pending", 500000, None)


def test_status_patch_is_admin_only(db, client):
    owner = make_patient(db)
    admin = make_admin(db)
    rx = make_prescription(db, owner)
    url = f"{BASE}/{rx.id}/status"
    body = {"status": "paid"}

    assert client.patch(url, json=body).status_code == 401
    assert client.patch(url, json=body, headers=auth_headers(owner.user)).status_code == 401
    db.refresh(rx)
    assert rx.status.value == "pending"

    assert client.patch(url, json=body, headers=auth_headers(admin)).status_code == 200
    db.refresh(rx)
    assert rx.status.value == "paid"


def test_upload_note_is_admin_only(db, client, tmp_path, monkeypatch):
    monkeypatch.setattr(prescription_endpoints, "UPLOAD_DIR", str(tmp_path))
    owner = make_patient(db)
    admin = make_admin(db)
    rx = make_prescription(db, owner)
    url = f"{BASE}/{rx.id}/upload_note"
    files = lambda: {"file": ("note.pdf", b"%PDF-1.4 test", "application/pdf")}  # noqa: E731

    assert client.post(url, files=files()).status_code == 401
    assert client.post(url, files=files(), headers=auth_headers(owner.user)).status_code == 401
    assert list(tmp_path.iterdir()) == []

    assert client.post(url, files=files(), headers=auth_headers(admin)).status_code == 200
    assert len(list(tmp_path.iterdir())) == 1


# --- other routes in the file -------------------------------------------------


def test_settings_and_availability_require_token(db, client, settings_row):
    patient = make_patient(db)
    for path in ("/settings", "/settings/latest", "/check_availability/paracetamol"):
        assert client.get(f"{BASE}{path}").status_code == 401, path
        assert client.get(f"{BASE}{path}", headers=auth_headers(patient.user)).status_code == 200, path
