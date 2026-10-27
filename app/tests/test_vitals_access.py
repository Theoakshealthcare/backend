"""Who may read and write a patient's vitals: the patient, and a caregiver only while a visit with that
patient is in progress (or the patient granted access). Nothing here changes the rule; it pins it, because
the caregiver app builds its Vitals tab on the 403 `Access denied` answer."""

from datetime import datetime, timedelta

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.patient_caregiver_access import PatientCaregiverAccess
from app.db.models.patient_vital import PatientVital
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient, make_user


def _vital_body(patient, recorder):
    return {"patient_id": str(patient.id), "recorded_by_user_id": str(recorder.id),
            "vital_type": "heart_rate", "value": "84", "unit": "bpm"}


def test_caregiver_reads_and_writes_vitals_during_an_in_progress_visit(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS)
    headers = auth_headers(caregiver.user)

    created = client.post("/api/v1/vitals/", json=_vital_body(patient, caregiver.user), headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["value"] == "84"

    listed = client.get(f"/api/v1/vitals/{patient.id}", headers=headers)
    assert listed.status_code == 200
    assert [v["value"] for v in listed.json()] == ["84"]


def test_caregiver_with_only_an_accepted_visit_is_refused(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    make_booking(db, patient, caregiver, status=BookingStatus.ACCEPTED)
    headers = auth_headers(caregiver.user)

    assert client.get(f"/api/v1/vitals/{patient.id}", headers=headers).json() == {"detail": "Access denied"}
    r = client.post("/api/v1/vitals/", json=_vital_body(patient, caregiver.user), headers=headers)
    assert r.status_code == 403 and r.json() == {"detail": "Access denied"}


def test_another_caregivers_in_progress_visit_gives_no_access(db, client):
    patient, caregiver, other = make_patient(db), make_caregiver(db), make_caregiver(db)
    make_booking(db, patient, other, status=BookingStatus.IN_PROGRESS)
    r = client.get(f"/api/v1/vitals/{patient.id}", headers=auth_headers(caregiver.user))
    assert r.status_code == 403


@pytest.mark.parametrize("status", [
    BookingStatus.PENDING, BookingStatus.EN_ROUTE, BookingStatus.COMPLETED,
    BookingStatus.CANCELLED, BookingStatus.DECLINED,
])
def test_no_other_visit_state_gives_a_caregiver_access(db, client, status):
    patient, caregiver = make_patient(db), make_caregiver(db)
    make_booking(db, patient, caregiver, status=status)
    r = client.get(f"/api/v1/vitals/{patient.id}", headers=auth_headers(caregiver.user))
    assert r.status_code == 403 and r.json() == {"detail": "Access denied"}


def test_a_grant_from_the_patient_gives_access_until_it_is_revoked(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    grant = PatientCaregiverAccess(patient_id=patient.id, caregiver_id=caregiver.id)
    db.add(grant)
    db.commit()
    headers = auth_headers(caregiver.user)
    assert client.get(f"/api/v1/vitals/{patient.id}", headers=headers).status_code == 200

    grant.revoked_at = datetime.utcnow()
    db.commit()
    assert client.get(f"/api/v1/vitals/{patient.id}", headers=headers).status_code == 403


def test_a_patient_reads_their_own_vitals_and_not_anothers(db, client):
    mine, other = make_patient(db), make_patient(db)
    assert client.get(f"/api/v1/vitals/{mine.id}", headers=auth_headers(mine.user)).status_code == 200
    assert client.get(f"/api/v1/vitals/{other.id}", headers=auth_headers(mine.user)).status_code == 403


def test_a_caregiver_without_a_profile_is_refused(db, client):
    patient = make_patient(db)
    user = make_user(db, "caregiver")  # a caregiver account that never created a profile
    assert client.get(f"/api/v1/vitals/{patient.id}", headers=auth_headers(user)).status_code == 403


def test_the_list_filters_by_start_and_the_report_downloads_as_csv(db, client):
    """What the caregiver app sends: `start` as UTC without an offset, newest first."""
    patient, caregiver = make_patient(db), make_caregiver(db)
    make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS)
    now = datetime.utcnow()
    for value, age_days in (("70", 40), ("84", 1)):
        row = PatientVital(patient_id=patient.id, recorded_by_user_id=caregiver.user_id, vital_type="heart_rate",
                           unit="bpm", recorded_at=now - timedelta(days=age_days))
        row.value = value
        db.add(row)
    db.commit()
    headers = auth_headers(caregiver.user)

    start = (now - timedelta(days=30)).replace(microsecond=0).isoformat()
    r = client.get(f"/api/v1/vitals/{patient.id}", params={"start": start}, headers=headers)
    assert r.status_code == 200 and [v["value"] for v in r.json()] == ["84"]
    assert len(client.get(f"/api/v1/vitals/{patient.id}", headers=headers).json()) == 2

    csv = client.get(f"/api/v1/vitals/{patient.id}/download", headers=headers)
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0].startswith("id,patient_id,recorded_by_user_id,source,vital_type,value")
