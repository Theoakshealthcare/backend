"""The vitals API now returns a clinician-approved flag with each reading (additive: nothing was removed or renamed).

The thresholds themselves are pinned in test_vital_flags.py; here the wiring is checked: every endpoint that returns a
reading carries `flag` and `glucose_context`, the patient's record changes the thresholds, weight and sleep carry no flag,
and the stored-reading hook for the alerts slice works."""

from datetime import datetime, timedelta

from app.db.models.booking import BookingStatus
from app.db.models.patient_vital import PatientVital
from app.services.vital_flags import patient_context, stored_reading_is_out_of_range
from app.tests.care_plan_helpers import create_plan, make_nurse, self_visit
from app.tests.factories import auth_headers, make_caregiver, make_patient

URL = "/api/v1/vitals/"


def post(client, patient, vital_type, value, unit, notes=None):
    payload = {"patient_id": str(patient.id), "recorded_by_user_id": str(patient.user_id), "vital_type": vital_type,
               "value": value, "unit": unit, "source": "manual"}
    if notes is not None:
        payload["notes"] = notes
    return client.post(URL, json=payload, headers=auth_headers(patient.user))


def stored(db, patient, vital_type, value, unit, notes=None, minutes_ago=0):
    row = PatientVital(patient_id=patient.id, recorded_by_user_id=patient.user_id, vital_type=vital_type, unit=unit,
                       recorded_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
    row.value = value
    row.notes = notes
    db.add(row)
    db.commit()
    return row


def test_create_returns_the_flag_and_keeps_every_old_field(db, client):
    patient = make_patient(db)
    r = post(client, patient, "heart_rate", "110", "bpm")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"id", "patient_id", "recorded_by_user_id", "vital_type", "value", "unit", "source", "recorded_at",
            "notes"} <= set(body)
    assert body["flag"] == {"status": "high", "label": "High", "emergency": False, "message": None,
                            "reason": "Heart rate above 100 bpm. Tell the family or the patient's doctor for a medical review."}
    assert body["glucose_context"] is None


def test_a_critical_reading_carries_the_112_message(db, client):
    patient = make_patient(db)
    flag = post(client, patient, "heart_rate", "45", "bpm").json()["flag"]
    assert flag["status"] == "critical" and flag["emergency"] is True
    assert flag["message"] == "This may be an emergency. Call 112 now."


def test_list_flags_every_reading_and_leaves_weight_and_sleep_unflagged(db, client):
    patient = make_patient(db)
    stored(db, patient, "heart_rate", "72", "bpm", minutes_ago=1)
    stored(db, patient, "blood_pressure", "150/95", "mmHg", minutes_ago=2)
    stored(db, patient, "sleep_duration", "5", "hrs", "22:30-03:30", minutes_ago=3)
    stored(db, patient, "weight", "250", "kg", minutes_ago=4)  # not one of the six: stored by older builds, listed, never flagged
    stored(db, patient, "heart_rate", "banana", "bpm", minutes_ago=5)  # a legacy row that was never validated
    rows = client.get(f"{URL}{patient.id}", headers=auth_headers(patient.user)).json()
    by_key = {(r["vital_type"], r["value"]): r["flag"] for r in rows}
    assert by_key[("heart_rate", "72")]["status"] == "normal"
    assert by_key[("blood_pressure", "150/95")]["status"] == "high"
    assert by_key[("sleep_duration", "5")] is None
    assert by_key[("weight", "250")] is None
    assert by_key[("heart_rate", "banana")] is None


def test_glucose_label_is_read_from_notes_and_changes_the_flag(db, client):
    patient = make_patient(db)
    fasting = post(client, patient, "blood_glucose", "130", "mg/dL", "FBS").json()
    random = post(client, patient, "blood_glucose", "130", "mg/dL", "RBS").json()
    after = post(client, patient, "blood_glucose", "190", "mg/dL", "PP").json()
    unlabelled = post(client, patient, "blood_glucose", "130", "mg/dL").json()
    assert (fasting["glucose_context"], fasting["flag"]["status"]) == ("fasting", "high")
    assert (random["glucose_context"], random["flag"]["status"]) == ("random", "normal")
    assert (after["glucose_context"], after["flag"]["status"]) == ("after_meal", "normal")
    assert (unlabelled["glucose_context"], unlabelled["flag"]["status"]) == (None, "normal")
    assert fasting["notes"] == "FBS"  # the stored label is unchanged


def test_update_re_rates_the_edited_reading(db, client):
    patient = make_patient(db)
    created = post(client, patient, "heart_rate", "72", "bpm").json()
    r = client.put(f"{URL}{created['id']}", json={"value": "130"}, headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    assert r.json()["flag"]["status"] == "high"


def test_diabetes_in_the_profile_lowers_the_blood_pressure_line(db, client):
    plain, diabetic = make_patient(db), make_patient(db, medical_conditions="Type 2 diabetes mellitus")
    assert post(client, plain, "blood_pressure", "135/78", "mmHg").json()["flag"]["status"] == "normal"
    flag = post(client, diabetic, "blood_pressure", "135/78", "mmHg").json()["flag"]
    assert flag["status"] == "high" and "130/80" in flag["reason"]


def test_the_active_care_plan_diagnosis_counts_too(db, client):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    assert create_plan(client, nurse, booking, diagnosis="Chronic kidney disease stage 3").status_code in (200, 201)
    assert patient_context(db, patient.id).lowers_bp_ceiling is True
    assert post(client, patient, "blood_pressure", "135/78", "mmHg").json()["flag"]["status"] == "high"


def test_the_reason_never_names_the_condition_behind_the_threshold(db, client):
    # A caregiver with a vitals grant may not be allowed to read the diagnosis or the medicines.
    patient = make_patient(db, medical_conditions="Hypertension, diabetes", medications="Metoprolol 50mg twice daily")
    low_hr = post(client, patient, "heart_rate", "58", "bpm").json()["flag"]
    high_bp = post(client, patient, "blood_pressure", "135/78", "mmHg").json()["flag"]
    assert low_hr["status"] == "low" and high_bp["status"] == "high"
    for flag in (low_hr, high_bp):
        text = (flag["reason"] or "").lower()
        assert not any(word in text for word in ("diabet", "kidney", "renal", "beta", "hypertens", "metoprolol"))
    assert patient_context(db, patient.id).beta_blocker_for_hypertension is True


def test_the_caregiver_sees_the_same_flags_during_a_visit(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    from app.tests.factories import make_booking
    make_booking(db, patient, caregiver, status=BookingStatus.IN_PROGRESS)
    stored(db, patient, "heart_rate", "120", "bpm")
    rows = client.get(f"{URL}{patient.id}", headers=auth_headers(caregiver.user)).json()
    assert rows[0]["flag"]["status"] == "high"


def test_the_hook_for_alerts_says_whether_a_stored_reading_is_out_of_range(db):
    patient = make_patient(db, medical_conditions="diabetes")
    assert stored_reading_is_out_of_range(db, stored(db, patient, "heart_rate", "72", "bpm")) is False
    assert stored_reading_is_out_of_range(db, stored(db, patient, "heart_rate", "30", "bpm")) is True
    assert stored_reading_is_out_of_range(db, stored(db, patient, "blood_pressure", "135/78", "mmHg")) is True  # 130/80 line
    assert stored_reading_is_out_of_range(db, stored(db, patient, "sleep_duration", "2", "hrs")) is False
    assert stored_reading_is_out_of_range(db, stored(db, patient, "blood_glucose", "140", "mg/dL", "FBS")) is True


def test_an_unknown_patient_gets_the_standard_thresholds(db):
    from uuid import uuid4
    ctx = patient_context(db, uuid4())
    assert (ctx.lowers_bp_ceiling, ctx.beta_blocker_for_hypertension) == (False, False)
