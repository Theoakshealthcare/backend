import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

# Helper to get a valid caregiver token (replace with your fixture or logic)
def get_caregiver_token():
    # This should be replaced with a real token retrieval or fixture
    return "test_caregiver_token"

def auth_headers(token=None):
    return {"Authorization": f"Bearer {token or get_caregiver_token()}"}

def test_update_profile_step1_success():
    response = client.put(
        "/api/v1/caregiver/profile/step1",
        headers=auth_headers(),
        json={
            "date_of_birth": "1990-01-01",
            "gender": "male",
            "state": "Lagos",
            "house_address": "123 Main Street",
            "emergency_contact_phone": "+2348012345678"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "Lagos"
    assert data["gender"] == "male"
    assert data["date_of_birth"] == "1990-01-01"

def test_update_profile_step1_invalid():
    response = client.put(
        "/api/v1/caregiver/profile/step1",
        headers=auth_headers(),
        json={
            # Missing required fields
        }
    )
    assert response.status_code == 422

def test_update_profile_step2_success():
    response = client.put(
        "/api/v1/caregiver/profile/step2",
        headers=auth_headers(),
        json={
            "specialization": "General Caregiving",
            "years_of_experience": 5,
            "preferred_work_hours": "Full-time",
            "service_areas": ["Lagos", "Abuja"]
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert data["specialization"] == "General Caregiving"
    assert data["years_of_experience"] == 5
    assert "service_areas" in data

def test_update_profile_step2_invalid():
    response = client.put(
        "/api/v1/caregiver/profile/step2",
        headers=auth_headers(),
        json={
            # Missing required fields
        }
    )
    assert response.status_code == 422

def test_get_profile():
    response = client.get(
        "/api/v1/caregiver/profile",
        headers=auth_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert "email" in data
    assert "profile_completion_step" in data

def test_get_form_options():
    response = client.get(
        "/api/v1/caregiver/form-options",
        headers=auth_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert "states" in data
    assert "specializations" in data
    assert "work_hours" in data
    assert "service_areas" in data 