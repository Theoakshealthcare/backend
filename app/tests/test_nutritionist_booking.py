import pytest
import requests
import uuid
from datetime import datetime, timedelta

BASE_URL = "http://localhost:8000/api/v1/booking"
PATIENT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NTE4NzM1MDcsInN1YiI6IjI2ZTA4YTBmLThkMmQtNGI4Ni05NTQ2LTJlMzk0MTI0ZGQwNiJ9.3el6L5t3_8q8JhE_kfWkk5CVuNftSZanAf26J6721Kk"
PATIENT_ID = "a1982f60-fffb-4372-8dbf-b62b418b903b"
HEADERS = {"Authorization": f"Bearer {PATIENT_TOKEN}"}
REAL_BOOKING_ID = "3888e9f9-a024-4ebf-8b3b-55a4a0b18901"

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

def test_get_nutritionist_booking():
    resp = requests.get(f"{BASE_URL}/booking/{REAL_BOOKING_ID}", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == REAL_BOOKING_ID
    assert data["service_type"] == "nutritionist" 