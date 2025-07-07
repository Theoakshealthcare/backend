import pytest
from fastapi.testclient import TestClient
from app.main import app
from datetime import datetime

client = TestClient(app)

# Real patient token and IDs provided by user
PATIENT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NTE4NzM1MDcsInN1YiI6IjI2ZTA4YTBmLThkMmQtNGI4Ni05NTQ2LTJlMzk0MTI0ZGQwNiJ9.3el6L5t3_8q8JhE_kfWkk5CVuNftSZanAf26J6721Kk"
PATIENT_ID = "a1982f60-fffb-4372-8dbf-b62b418b903b"
CAREVER_ID = "136b7ca6-1a29-4204-b7ec-863daf03c207"
BOOKING_ID = "3888e9f9-a024-4ebf-8b3b-55a4a0b18901"
USER_ID = "26e08a0f-8d2d-4b86-9546-2e394124dd06"  # Provided user_id (patient)

# Helper for auth headers
def auth_header(token=PATIENT_TOKEN):
    return {"Authorization": f"Bearer {token}"}

@pytest.fixture
def vital_payload():
    return {
        "patient_id": PATIENT_ID,
        "recorded_by_user_id": USER_ID,
        "vital_type": "blood_pressure",
        "value": "120/80",
        "unit": "mmHg",
        "source": "manual",
        "recorded_at": datetime.utcnow().isoformat(),
        "notes": "Test vital"
    }

def test_create_vital_as_patient(vital_payload):
    response = client.post("/api/v1/vitals/", json=vital_payload, headers=auth_header())
    assert response.status_code in (200, 201)
    data = response.json()
    assert data["vital_type"] == "blood_pressure"
    global VITAL_ID
    VITAL_ID = data["id"]

def test_list_vitals_as_patient():
    response = client.get(f"/api/v1/vitals/{PATIENT_ID}", headers=auth_header())
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)

def test_update_vital_as_patient():
    update = {"value": "130/85"}
    response = client.put(f"/api/v1/vitals/{VITAL_ID}", json=update, headers=auth_header())
    assert response.status_code == 200
    data = response.json()
    assert data["value"] == "130/85"

def test_delete_vital_as_patient():
    response = client.delete(f"/api/v1/vitals/{VITAL_ID}", headers=auth_header())
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True

def test_download_vitals_csv_as_patient():
    response = client.get(f"/api/v1/vitals/{PATIENT_ID}/download", headers=auth_header())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv") 