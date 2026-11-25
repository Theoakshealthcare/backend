"""Writes the live captures behind docs/fe-integration-guide-vitals-flags.md.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_VITALS_FLAGS=1 poetry run pytest app/tests/test_capture_vitals_flags.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-vitals-flags.md docs/fe-captures/vitals-flags`.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.patient_vital import PatientVital
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_VITALS_FLAGS"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "vitals-flags"


def _save(name, r, note=None, body=None):
    doc = {
        "request": {"method": r.request.method, "path": r.request.url.path, "auth": "Bearer <ACCESS_TOKEN_JWT>",
                    "body": body},
        "response": {"status_code": r.status_code, "body": r.json()},
    }
    if note:
        doc["note"] = note
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _post(client, patient, name, vital_type, value, unit, notes=None, note=None):
    payload = {"patient_id": str(patient.id), "recorded_by_user_id": str(patient.user_id), "vital_type": vital_type,
               "value": value, "unit": unit, "source": "manual"}
    if notes is not None:
        payload["notes"] = notes
    r = client.post("/api/v1/vitals/", json=payload, headers=auth_headers(patient.user))
    _save(name, r, note, {**payload, "patient_id": "<PATIENT_ID>", "recorded_by_user_id": "<USER_ID>"})
    assert r.status_code == 200, r.text
    return r.json()


def test_capture(db, client):
    patient = make_patient(db, full_name="Placeholder Patient", email="vitals-flags@example.com",
                           phone_number="+2340000719101")
    diabetic = make_patient(db, full_name="Placeholder Diabetic", email="vitals-flags-dm@example.com",
                            phone_number="+2340000719102", medical_conditions="Type 2 diabetes mellitus")
    headers = auth_headers(patient.user)

    _post(client, patient, "001-create-heart-rate-high.json", "heart_rate", "110", "bpm")
    _post(client, patient, "002-create-heart-rate-critical.json", "heart_rate", "45", "bpm",
          note="Critical: emergency is true and message is set.")
    _post(client, patient, "003-create-blood-pressure-normal.json", "blood_pressure", "128/82", "mmHg")
    _post(client, patient, "004-create-blood-pressure-low.json", "blood_pressure", "96/58", "mmHg")
    _post(client, patient, "005-create-blood-pressure-critical.json", "blood_pressure", "85/50", "mmHg")
    _post(client, patient, "006-create-temperature-high-fahrenheit.json", "temperature", "100.4", "°F")
    _post(client, patient, "007-create-glucose-fasting-high.json", "blood_glucose", "130", "mg/dL", "FBS")
    _post(client, patient, "008-create-glucose-random-same-value-normal.json", "blood_glucose", "130", "mg/dL", "RBS")
    _post(client, patient, "009-create-glucose-no-label.json", "blood_glucose", "130", "mg/dL")
    _post(client, patient, "010-create-sleep-no-flag.json", "sleep_duration", "5", "hrs", "22:30-03:30",
          note="Sleep (and weight) are never flagged: flag is null, not absent.")
    _post(client, diabetic, "011-create-blood-pressure-diabetic-high.json", "blood_pressure", "135/78", "mmHg",
          note="Same 135/78 would be Normal for a patient without diabetes or kidney disease.")
    _post(client, patient, "012-create-blood-pressure-same-value-plain-normal.json", "blood_pressure", "135/78", "mmHg")

    created = _post(client, patient, "013-create-heart-rate-for-edit.json", "heart_rate", "72", "bpm")
    r = client.put(f"/api/v1/vitals/{created['id']}", json={"value": "130"}, headers=headers)
    _save("014-update-heart-rate-re-rated.json", r, "Editing the value re-rates the reading.", {"value": "130"})

    r = client.get(f"/api/v1/vitals/{patient.id}", headers=headers)
    _save("015-list-mixed.json", r, "Newest first; every row carries flag and glucose_context.")

    caregiver = make_caregiver(db)
    make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    booking = make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    r = client.get(f"/api/v1/caregiver/bookings/{booking.id}/client", headers=auth_headers(caregiver.user))
    _save("016-caregiver-client-profile-latest-vitals.json", r,
          "latest_vitals items carry flag and glucose_context.")
