import pytest
import requests
import uuid
from datetime import datetime, timedelta

BASE_URL = "http://localhost:8000/api/v1/booking"
PATIENT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NTE4MTU4NTMsInN1YiI6IjI2ZTA4YTBmLThkMmQtNGI4Ni05NTQ2LTJlMzk0MTI0ZGQwNiJ9.vSFVEnEe3_MFcFtzlT4M4tng4biYi74nWbb3eMlbYHU"
HEADERS = {"Authorization": f"Bearer {PATIENT_TOKEN}"}

PATIENT_ID = "a1982f60-fffb-4372-8dbf-b62b418b903b"

@pytest.fixture(scope="module")
def booking_data():
    now = datetime.utcnow() + timedelta(days=1)
    return {
        "service_type": "nutritionist",
        "assistance_type": "consultation",
        "condition": "diabetes",
        "duration_hours": 1,
        "scheduled_date": now.isoformat(),
        "patient_address": "NO 3 Gbagada street lagos state",
        "patient_latitude": 6.555,
        "patient_longitude": 3.333,
        "patient_city": "Lagos",
        "patient_state": "Lagos",
        "special_instructions": "Virtual consult (video call)",
        "booking_for": "myself"
    }

@pytest.fixture(scope="module")
def created_booking(booking_data):
    resp = requests.post(f"{BASE_URL}/create", json=booking_data, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["id"]

def test_create_nutritionist_booking(created_booking):
    assert created_booking is not None
    assert isinstance(created_booking, str)

def test_get_nutritionist_booking(created_booking):
    resp = requests.get(f"{BASE_URL}/{created_booking}", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == created_booking
    assert data["service_type"] == "nutritionist" 