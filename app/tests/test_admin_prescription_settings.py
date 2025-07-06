import pytest
import requests
import uuid

BASE_URL = "http://localhost:8000/api/v1/prescription/settings"
ADMIN_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3NTE4MTUzMTgsInN1YiI6IjlkOGJhMjNhLTgzOTItNDk3OC04OTVlLTVjMDgzMGMwNTg5MyJ9.qAsIUR3Qw51Or1Sh7S7LM-ubOmWUdlvhGPbMlenIivU"
HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

@pytest.fixture(scope="module")
def settings_data():
    return {
        "base_fee": 10000,
        "multipliers": {"urgent": 2.0, "regular": 1.0},
        "surge_pricing": True,
        "surge_multiplier": 1.5,
        "discount": 10.0
    }

def test_create_prescription_settings(settings_data):
    resp = requests.post(BASE_URL, json=settings_data, headers=HEADERS)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    global SETTINGS_ID
    SETTINGS_ID = data["id"]
    assert data["base_fee"] == 10000
    assert data["multipliers"]["urgent"] == 2.0

def test_get_all_prescription_settings():
    resp = requests.get(BASE_URL, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert any(s["id"] == SETTINGS_ID for s in data)

def test_get_latest_prescription_settings():
    resp = requests.get(BASE_URL + "/latest", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == SETTINGS_ID

def test_update_prescription_settings():
    update = {"base_fee": 12000, "discount": 20.0}
    resp = requests.put(f"{BASE_URL}/{SETTINGS_ID}", json=update, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["base_fee"] == 12000
    assert data["discount"] == 20.0

def test_delete_prescription_settings():
    resp = requests.delete(f"{BASE_URL}/{SETTINGS_ID}", headers=HEADERS)
    assert resp.status_code == 204, resp.text 