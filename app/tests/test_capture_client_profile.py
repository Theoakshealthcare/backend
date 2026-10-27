"""Writes the live captures behind the client profile section of the caregiver FE guide.

Skipped in normal runs. Run it against the real app and a real Postgres (the test harness):

    CAPTURE_CLIENT_PROFILE=1 poetry run pytest app/tests/test_capture_client_profile.py -q

then `poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver.md docs/fe-captures/caregiver`.
It only writes the 19x files; scripts/capture_caregiver_flow.py (compose stack) rewrites the whole directory,
so fold these calls into it the next time it is run.
"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care import CareRecipient
from app.db.models.patient import PatientMedicalRecord
from app.db.models.patient_vital import PatientVital
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

pytestmark = pytest.mark.skipif(not os.environ.get("CAPTURE_CLIENT_PROFILE"), reason="capture run only")
OUT = Path(__file__).resolve().parents[2] / "docs" / "fe-captures" / "caregiver"


def _save(name, r, note=None):
    doc = {
        "request": {"method": "GET", "path": r.request.url.path, "auth": "Bearer <ACCESS_TOKEN_JWT>", "body": None},
        "response": {"status_code": r.status_code,
                     "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control")},
                     "body": r.json()},
    }
    if note:
        doc["note"] = note
    (OUT / name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _vital(db, patient, user_id, kind, value, unit, minutes_ago):
    row = PatientVital(patient_id=patient.id, recorded_by_user_id=user_id, vital_type=kind, unit=unit,
                       recorded_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
    row.value = value
    db.add(row)
    db.commit()


def test_capture(db, client):
    patient = make_patient(db, full_name="Placeholder Patient", email="patient-profile@example.com",
                           phone_number="+2340000718101", date_of_birth=date.today().replace(year=date.today().year - 70) - timedelta(days=10),
                           gender="female", blood_group="O+", allergies="Placeholder allergy",
                           medical_conditions="Placeholder condition", medications="Placeholder medication",
                           emergency_contact_name="Placeholder Kin", emergency_contact_phone="+2340000718102",
                           emergency_contact_relationship="Daughter")
    caregiver, other = make_caregiver(db), make_caregiver(db)
    db.add(PatientMedicalRecord(patient_id=patient.id, record_type="diagnosis", title="Placeholder diagnosis",
                                description="Placeholder note", severity="medium", date_recorded=date(2019, 5, 1)))
    db.commit()
    _vital(db, patient, caregiver.user_id, "heart_rate", "84", "bpm", 15)
    _vital(db, patient, caregiver.user_id, "blood_pressure", "138/85", "mmHg", 18)
    headers = auth_headers(caregiver.user)
    url = "/api/v1/caregiver/bookings/{}/client"

    live = make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS, booking_for="myself")
    _save("190-a-client-profile-in-progress.json", client.get(url.format(live.id), headers=headers),
          "Booking in progress: vitals are available (latest of each type).")

    patient2 = make_patient(db, full_name="Placeholder Patient Two", gender="male")
    accepted = make_booking(db, patient2, caregiver, status=BookingStatus.ACCEPTED, booking_for="myself")
    _save("191-a-client-profile-accepted.json", client.get(url.format(accepted.id), headers=headers),
          "Booking only accepted: latest_vitals is null (vitals need an in-progress visit or a patient grant).")

    booker = make_patient(db, full_name="Placeholder Booker", phone_number="+2340000718103")
    recipient = CareRecipient(full_name="Placeholder Relative", date_of_birth=date(1944, 1, 1), gender="female",
                              created_by_user_id=booker.user_id)
    db.add(recipient)
    db.commit()
    for_other = make_booking(db, booker, caregiver, status=BookingStatus.IN_PROGRESS, booking_for="mother",
                             channel="whatsapp", booked_by_user_id=booker.user_id, care_recipient_id=recipient.id)
    _save("192-a-client-profile-for-someone-else.json", client.get(url.format(for_other.id), headers=headers),
          "Booked by a family member for someone else: the recipient's name, age and sex; no medical history, "
          "photo or vitals (they belong to the booker); the booker is the family contact.")

    _save("193-a-client-profile-other-caregiver.json", client.get(url.format(live.id), headers=auth_headers(other.user)),
          "Another caregiver's booking: the same 404 as for a booking that does not exist.")
    done = make_booking(db, patient, caregiver, status=BookingStatus.COMPLETED)
    _save("194-a-client-profile-completed.json", client.get(url.format(done.id), headers=headers),
          "A finished visit: no access.")
    _save("195-a-client-profile-patient-token.json", client.get(url.format(live.id), headers=auth_headers(patient.user)))
