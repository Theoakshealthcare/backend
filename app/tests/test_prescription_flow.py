import os
import uuid
import pytest
import requests

BASE_URL = "http://localhost:8000/api/v1"
PATIENT_ID = "a1982f60-fffb-4372-8dbf-b62b418b903b"
USER_ID = "26e08a0f-8d2d-4b86-9546-2e394124dd06"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NTE4NzM1MDcsInN1YiI6IjI2ZTA4YTBmLThkMmQtNGI4Ni05NTQ2LTJlMzk0MTI0ZGQwNiJ9.3el6L5t3_8q8JhE_kfWkk5CVuNftSZanAf26J6721Kk"
HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}
ADMIN_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NTE4NzM2NTQsInN1YiI6IjlkOGJhMjNhLTgzOTItNDk3OC04OTVlLTVjMDgzMGMwNTg5MyJ9.BnavsmHKuIDhzfF5H8TIf_Uki1UqdsetiNByXoEKsgo"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

@pytest.fixture(scope="module", autouse=True)
def ensure_prescription_settings():
    settings_url = f"{BASE_URL}/prescription/settings"
    settings_data = {
        "base_fee": 10000,
        "multipliers": {"urgent": 2.0, "regular": 1.0, "Home Delivery": 1.0},
        "surge_pricing": True,
        "surge_multiplier": 1.5,
        "discount": 10.0
    }
    resp = requests.post(settings_url, json=settings_data, headers=ADMIN_HEADERS)
    # Accept 201 (created) or 409 (already exists)
    assert resp.status_code in (201, 409), resp.text

@pytest.fixture(scope="module")
def prescription_data():
    return {
        "patient_id": PATIENT_ID,
        "requested_by_user_id": USER_ID,
        "medication_name": "Felvin",
        "dosage": "500mg",
        "frequency": "Once Daily",
        "duration": "3 Months",
        "reason": "Chronic Disease",
        "additional_info": "Take after meal",
        "delivery_option": "Home Delivery",
        "address": "NO 3 Gbagada street lagos state"
    }

@pytest.fixture(scope="module")
def created_prescription(prescription_data):
    resp = requests.post(f"{BASE_URL}/prescription/create", json=prescription_data, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    prescription_id = data["id"]
    paystack_ref = str(uuid.uuid4())
    return {"id": prescription_id, "paystack_ref": paystack_ref}

def test_create_prescription(created_prescription):
    assert "id" in created_prescription
    assert "paystack_ref" in created_prescription

def test_upload_doctor_note(created_prescription):
    prescription_id = created_prescription["id"]
    file_path = "dummy_note.txt"
    with open(file_path, "w") as f:
        f.write("Doctor's note for testing.")
    with open(file_path, "rb") as f:
        files = {"file": (file_path, f, "text/plain")}
        resp = requests.post(f"{BASE_URL}/prescription/{prescription_id}/upload_note", files=files, headers=HEADERS)
    os.remove(file_path)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "doctor_note_path" in data and data["doctor_note_path"]

def test_check_drug_availability():
    resp = requests.get(f"{BASE_URL}/prescription/check_availability/Felvin")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["available"] is True
    assert data["medication_name"] == "Felvin"

def test_update_paystack_reference(created_prescription):
    prescription_id = created_prescription["id"]
    paystack_ref = created_prescription["paystack_ref"]
    resp = requests.patch(f"{BASE_URL}/prescription/{prescription_id}", json={"paystack_reference": paystack_ref}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["paystack_reference"] == paystack_ref

def test_simulate_payment(created_prescription):
    prescription_id = created_prescription["id"]
    paystack_ref = created_prescription["paystack_ref"]
    payload = {
        "event": "charge.success",
        "data": {"reference": paystack_ref}
    }
    resp = requests.post(f"{BASE_URL}/payment/paystack/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    resp2 = requests.get(f"{BASE_URL}/prescription/{prescription_id}", headers=HEADERS)
    assert resp2.status_code == 200, resp2.text
    data = resp2.json()
    assert data["payment_status"] == "paid"

def test_update_and_get_status(created_prescription):
    prescription_id = created_prescription["id"]
    resp = requests.patch(f"{BASE_URL}/prescription/{prescription_id}/status", json={"status": "delivering"}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "delivering"
    resp2 = requests.get(f"{BASE_URL}/prescription/{prescription_id}/status", headers=HEADERS)
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["status"] == "delivering" 