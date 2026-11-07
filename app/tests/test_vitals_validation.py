"""POST/PUT /vitals: only the six types the two apps use, in their units, within plausible ranges (DP1-16).

The accepted payloads below are what the caregiver app and the patient app send today, copied from their models and
from the patient app's live capture; the refused ones include the captured `300/20` and `banana`. Rows saved before
the rules existed must still list."""

import pytest

from app.db.models.booking import BookingStatus
from app.db.models.patient_vital import PatientVital
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

URL = "/api/v1/vitals/"


def body(patient, vital_type, value, unit, notes=None, **extra):
    payload = {"patient_id": str(patient.id), "recorded_by_user_id": str(patient.user_id), "vital_type": vital_type,
               "value": value, "unit": unit, "source": "manual", **extra}
    if notes is not None:
        payload["notes"] = notes
    return payload


def post(client, patient, *args, **kwargs):
    return client.post(URL, json=body(patient, *args, **kwargs), headers=auth_headers(patient.user))


ACCEPTED = [
    ("blood_pressure", "120/80", "mmHg", None),
    ("blood_pressure", "138/85", "mmHg", None),
    ("heart_rate", "72", "bpm", None),
    ("temperature", "36.8", "°C", None),
    ("temperature", "98.6", "°F", None),
    ("respiratory_rate", "16", "breaths/min", None),
    ("blood_glucose", "110", "mg/dL", "RBS"),
    ("blood_glucose", "92", "mg/dL", "FBS"),
    ("blood_glucose", "7.8", "mmol/L", "PP"),
    ("blood_glucose", "110", "mg/dL", None),
    ("sleep_duration", "7.5", "hrs", "22:30-06:00"),
    ("sleep_duration", "24", "hrs", None),
    ("heart_rate", "72", None, None),  # the unit was always optional
]


@pytest.mark.parametrize("vital_type,value,unit,notes", ACCEPTED)
def test_what_the_apps_send_today_is_still_accepted(db, client, vital_type, value, unit, notes):
    patient = make_patient(db)
    r = post(client, patient, vital_type, value, unit, notes)
    assert r.status_code == 200, r.text
    assert (r.json()["vital_type"], r.json()["value"], r.json()["unit"], r.json()["notes"]) == (vital_type, value, unit, notes)


def test_the_caregiver_app_can_still_record_during_a_visit(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS)
    payload = {"patient_id": str(patient.id), "recorded_by_user_id": str(caregiver.user_id), "vital_type": "blood_glucose",
               "value": "6.4", "unit": "mmol/L", "notes": "FBS"}
    r = client.post(URL, json=payload, headers=auth_headers(caregiver.user))
    assert r.status_code == 200, r.text


REFUSED = [
    # (type, value, unit, part of the message)
    ("banana", "not a number", "kg", "vital_type must be one of"),
    ("sleep", "7", "hrs", "vital_type must be one of"),  # the old guess: the key is sleep_duration
    ("blood_pressure", "300/20", "mmHg", "systolic"),
    ("blood_pressure", "120/120", "mmHg", "higher than"),
    ("blood_pressure", "80/120", "mmHg", "higher than"),
    ("blood_pressure", "120/10", "mmHg", "diastolic"),
    ("blood_pressure", "120", "mmHg", "120/80"),
    ("blood_pressure", "abc/80", "mmHg", "120/80"),
    ("blood_pressure", "120/80/60", "mmHg", "120/80"),
    ("blood_pressure", "120/80", "bpm", "mmHg"),
    ("heart_rate", "300", "bpm", "at most 250"),
    ("heart_rate", "5", "bpm", "at least 20"),
    ("heart_rate", "-72", "bpm", "number"),
    ("heart_rate", "7e2", "bpm", "number"),
    ("heart_rate", "72,5", "bpm", "number"),
    ("heart_rate", "", "bpm", "number"),
    ("heart_rate", "72", "mmHg", "bpm"),
    ("temperature", "98.6", "°C", "at most 45"),
    ("temperature", "37", "°F", "at least 86"),
    ("temperature", "36.8", "kelvin", "°C or °F"),
    ("respiratory_rate", "2", "breaths/min", "at least 4"),
    ("blood_glucose", "700", "mg/dL", "at most 600"),
    ("blood_glucose", "0.5", "mmol/L", "at least 1.1"),
    ("sleep_duration", "0", "hrs", "more than 0"),
    ("sleep_duration", "25", "hrs", "at most 24"),
]


@pytest.mark.parametrize("vital_type,value,unit,message", REFUSED)
def test_implausible_readings_are_422_with_a_sentence(db, client, vital_type, value, unit, message):
    patient = make_patient(db)
    r = post(client, patient, vital_type, value, unit)
    assert r.status_code == 422, r.text
    msg = r.json()["detail"][0]["msg"]  # the first msg is what both apps show
    assert message in msg and not msg.startswith("Value error"), msg
    assert db.query(PatientVital).filter(PatientVital.patient_id == patient.id).count() == 0


def test_overlong_notes_are_refused(db, client):
    patient = make_patient(db)
    r = post(client, patient, "heart_rate", "72", "bpm", notes="x" * 501)
    assert r.status_code == 422 and "notes" in r.json()["detail"][0]["msg"]


def test_the_value_is_stored_trimmed_and_blood_pressure_tidied(db, client):
    patient = make_patient(db)
    assert post(client, patient, "heart_rate", " 72 ", "bpm").json()["value"] == "72"
    assert post(client, patient, "blood_pressure", "120 / 80", "mmHg").json()["value"] == "120/80"


def test_the_access_rules_still_come_first_for_a_valid_reading(db, client):
    patient, other = make_patient(db), make_patient(db)
    r = client.post(URL, json=body(other, "heart_rate", "72", "bpm"), headers=auth_headers(patient.user))
    assert r.status_code == 403 and r.json() == {"detail": "Access denied"}


def test_rows_saved_before_the_rules_still_list_download_and_delete(db, client):
    patient = make_patient(db)
    legacy = []
    for vital_type, value, unit in (("banana", "not a number", "kg"), ("blood_pressure", "300/20", "mmHg")):
        row = PatientVital(patient_id=patient.id, recorded_by_user_id=patient.user_id, vital_type=vital_type, unit=unit)
        row.value = value
        db.add(row)
        legacy.append(row)
    db.commit()
    headers = auth_headers(patient.user)
    listed = client.get(f"{URL}{patient.id}", headers=headers)
    assert listed.status_code == 200
    assert {v["value"] for v in listed.json()} == {"not a number", "300/20"}
    assert client.get(f"{URL}{patient.id}/download", headers=headers).status_code == 200
    assert client.delete(f"{URL}{legacy[0].id}", headers=headers).status_code == 200


def test_an_edit_is_judged_on_the_whole_reading(db, client):
    patient = make_patient(db)
    created = post(client, patient, "temperature", "36.8", "°C").json()
    headers = auth_headers(patient.user)
    put = lambda changes: client.put(f"{URL}{created['id']}", json=changes, headers=headers)  # noqa: E731

    assert put({"value": "37.2"}).status_code == 200
    r = put({"value": "300"})
    assert r.status_code == 422 and "at most 45" in r.json()["detail"][0]["msg"]
    assert put({"value": "98.6"}).status_code == 422  # still °C
    assert put({"value": "98.6", "unit": "°F"}).status_code == 200
    assert put({"vital_type": "banana"}).status_code == 422
    assert put({"vital_type": "heart_rate"}).status_code == 422  # 98.6 °F is not a heart rate
    assert put({"notes": "after a walk"}).json()["notes"] == "after a walk"


def test_editing_only_the_notes_of_a_legacy_row_is_allowed(db, client):
    patient = make_patient(db)
    row = PatientVital(patient_id=patient.id, recorded_by_user_id=patient.user_id, vital_type="blood_pressure", unit="mmHg")
    row.value = "300/20"
    db.add(row)
    db.commit()
    r = client.put(f"{URL}{row.id}", json={"notes": "recheck"}, headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.json()["notes"] == "recheck"
