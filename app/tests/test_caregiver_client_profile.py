"""GET /api/v1/caregiver/bookings/{booking_id}/client: the client profile a caregiver sees for a visit.

Allowed only for the assigned caregiver of an accepted, on-the-way or in-progress booking. Everyone and
everything else answers the same 404, so the endpoint never says whether a booking or a patient exists.
Only what the profile screen needs is returned: no address, coordinates, email, phone number, booking
condition or special instructions.
"""

import uuid
from datetime import date, datetime, timedelta

import pytest

from app.core.security import encrypt
from app.db.models.booking import BookingNotification, BookingStatus, PaymentStatus
from app.db.models.care import CareRecipient
from app.db.models.patient import PatientMedicalRecord
from app.db.models.patient_caregiver_access import PatientCaregiverAccess
from app.db.models.patient_vital import PatientVital
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

NOT_FOUND = {"detail": "Client not found"}
URL = "/api/v1/caregiver/bookings/{}/client"

# Things the caregiver's profile screen must never carry.
SECRET_BITS = ("12 Secret Close", "6.447412", "3.472311", "gate code 4321", "insulin-dependent",
               "+2348000000999", "ada.secret@example.com")


def _years_ago(years: int, extra_days: int = 10) -> date:
    today = date.today()
    return date(today.year - years, today.month, today.day) - timedelta(days=extra_days)


def _patient(db, **over):
    fields = dict(full_name="Ada Patient", email="ada.secret@example.com", phone_number="+2348000000999",
                  date_of_birth=_years_ago(70), gender="female", address="12 Secret Close, Lekki",
                  postal_code="101233", emergency_contact_name="Next of Kin",
                  emergency_contact_phone="+2348000000888", emergency_contact_relationship="Daughter",
                  emergency_contact_address="9 Kin Street")
    fields.update(over)
    return make_patient(db, **fields)


def _booking(db, patient, caregiver, status=BookingStatus.ACCEPTED, **over):
    fields = dict(patient_address="12 Secret Close, Lekki Phase 1", patient_latitude=6.447412,
                  patient_longitude=3.472311, condition="insulin-dependent diabetes",
                  special_instructions="gate code 4321")
    fields.update(over)
    return make_booking(db, patient, caregiver, status=status, **fields)


def _get(client, booking, caregiver):
    return client.get(URL.format(booking.id), headers=auth_headers(caregiver.user))


# --- who may see it -------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS])
def test_the_assigned_caregiver_sees_the_profile_while_the_visit_is_live(db, client, status):
    patient, caregiver = _patient(db), make_caregiver(db)
    booking = _booking(db, patient, caregiver, status)
    r = _get(client, booking, caregiver)
    assert r.status_code == 200, r.text
    assert r.json()["booking_id"] == str(booking.id) and r.json()["patient_id"] == str(patient.id)
    assert r.json()["full_name"] == "Ada Patient"


@pytest.mark.parametrize("status", [BookingStatus.PENDING, BookingStatus.COMPLETED, BookingStatus.CANCELLED,
                                    BookingStatus.DECLINED])
def test_no_other_booking_state_gives_access(db, client, status):
    patient, caregiver = _patient(db), make_caregiver(db)
    booking = _booking(db, patient, caregiver, status)
    r = _get(client, booking, caregiver)
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_another_caregiver_gets_the_same_404_as_for_a_booking_that_does_not_exist(db, client):
    patient, mine, other = _patient(db), make_caregiver(db), make_caregiver(db)
    booking = _booking(db, patient, mine, BookingStatus.IN_PROGRESS)
    r = _get(client, booking, other)
    assert r.status_code == 404 and r.json() == NOT_FOUND
    missing = client.get(URL.format(uuid.uuid4()), headers=auth_headers(other.user))
    assert missing.status_code == 404 and missing.json() == r.json()


def test_a_caregiver_who_was_only_offered_the_job_sees_nothing(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    booking = _booking(db, patient, None, BookingStatus.PENDING, payment_status=PaymentStatus.PAID)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="New visit request",
                               message="New nurse visit in Lagos", notification_type="new_booking"))
    db.commit()
    r = _get(client, booking, caregiver)
    assert r.status_code == 404 and r.json() == NOT_FOUND
    for secret in SECRET_BITS + ("Ada Patient",):
        assert secret not in r.text


def test_a_caregiver_who_dropped_out_loses_access(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    booking = _booking(db, patient, caregiver, BookingStatus.ACCEPTED)
    assert _get(client, booking, caregiver).status_code == 200
    booking.caregiver_id, booking.status = None, BookingStatus.PENDING
    db.commit()
    assert _get(client, booking, caregiver).status_code == 404


def test_a_suspended_caregiver_may_not_start_a_new_visit_but_finishes_one_under_way(db, client):
    patient, caregiver = _patient(db), make_caregiver(db, profile_status="suspended")
    not_started = _booking(db, patient, caregiver, BookingStatus.ACCEPTED)
    on_way = _booking(db, patient, caregiver, BookingStatus.EN_ROUTE)
    under_way = _booking(db, patient, caregiver, BookingStatus.IN_PROGRESS)
    assert _get(client, not_started, caregiver).status_code == 404
    assert _get(client, on_way, caregiver).status_code == 404
    assert _get(client, under_way, caregiver).status_code == 200


def test_an_erased_patient_is_not_shown(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    booking = _booking(db, patient, caregiver, BookingStatus.ACCEPTED)
    # What account erasure leaves: the person's data wiped and the account closed.
    patient.full_name, patient.is_active = "Deleted user", False
    patient.date_of_birth = patient.gender = patient.emergency_contact_name = patient.emergency_contact_phone = None
    db.commit()
    r = _get(client, booking, caregiver)
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_other_account_types_and_anonymous_callers_are_refused(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    booking = _booking(db, patient, caregiver)
    assert client.get(URL.format(booking.id)).status_code == 401
    r = client.get(URL.format(booking.id), headers=auth_headers(patient.user))
    assert r.status_code == 403 and r.json() == {"detail": "Access denied. Caregiver account required."}
    assert client.get(URL.format("not-a-uuid"), headers=auth_headers(caregiver.user)).status_code == 422


def test_a_caregiver_account_without_a_profile_is_refused(db, client):
    from app.tests.factories import make_user

    patient = _patient(db)
    booking = _booking(db, patient, make_caregiver(db))
    user = make_user(db, "caregiver")
    r = client.get(URL.format(booking.id), headers=auth_headers(user))
    assert r.status_code == 404


# --- what it contains -----------------------------------------------------------------------------

ALLOWED_KEYS = {"booking_id", "patient_id", "full_name", "profile_image_url", "booking_for", "age", "gender",
                "emergency_contact", "medical_history", "latest_vitals"}


def test_the_profile_carries_only_what_the_screen_needs(db, client):
    patient = _patient(db, blood_group="O+", allergies="Penicillin", medical_conditions="Type 2 diabetes",
                       medications="Metformin")
    caregiver = make_caregiver(db)
    db.add(PatientMedicalRecord(patient_id=patient.id, record_type="diagnosis", title="Hypertension",
                                description="Since 2019", severity="medium", date_recorded=date(2019, 5, 1),
                                doctor_name="Dr Hidden", hospital_clinic="Hidden Clinic",
                                file_path="/secret/path.pdf", file_name="scan.pdf"))
    db.commit()
    booking = _booking(db, patient, caregiver, BookingStatus.IN_PROGRESS)

    r = _get(client, booking, caregiver)
    body = r.json()
    assert set(body) == ALLOWED_KEYS
    assert "care_plan" not in body  # care plans are not built
    assert body["age"] == 70 and body["gender"] == "female" and body["booking_for"] == "myself"
    assert body["emergency_contact"] == {"name": "Next of Kin", "phone": "+2348000000888", "relationship": "Daughter"}
    assert body["medical_history"] == {
        "blood_group": "O+", "allergies": "Penicillin", "medical_conditions": "Type 2 diabetes",
        "medications": "Metformin",
        "records": [{"record_type": "diagnosis", "title": "Hypertension", "description": "Since 2019",
                     "severity": "medium", "date_recorded": "2019-05-01"}],
    }
    for secret in SECRET_BITS + ("Dr Hidden", "Hidden Clinic", "/secret/path.pdf", "scan.pdf", "9 Kin Street",
                                 "101233"):
        assert secret not in r.text, f"profile leaks {secret!r}"
    assert "latitude" not in r.text and "longitude" not in r.text and "address" not in r.text


def test_a_whatsapp_condition_never_travels_in_the_profile(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    booking = _booking(db, patient, caregiver, condition=None, condition_encrypted=encrypt("post-stroke care"))
    assert "post-stroke" not in _get(client, booking, caregiver).text


def test_medical_history_and_emergency_contact_are_null_when_nothing_is_stored(db, client):
    patient = _patient(db, date_of_birth=None, gender=None, emergency_contact_name=None,
                       emergency_contact_phone=None, emergency_contact_relationship=None)
    caregiver = make_caregiver(db)
    body = _get(client, _booking(db, patient, caregiver), caregiver).json()
    assert body["age"] is None and body["gender"] is None
    assert body["emergency_contact"] is None and body["medical_history"] is None


def test_age_counts_whole_years(db, client):
    caregiver = make_caregiver(db)
    today = date.today()
    tomorrows_birthday = date(today.year - 40, today.month, today.day) + timedelta(days=1) \
        if not (today.month == 2 and today.day == 29) else date(today.year - 40, 3, 1)
    patient = _patient(db, date_of_birth=tomorrows_birthday)
    assert _get(client, _booking(db, patient, caregiver), caregiver).json()["age"] == 39


def test_the_response_is_not_cacheable(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    r = _get(client, _booking(db, patient, caregiver), caregiver)
    assert r.headers["cache-control"] == "no-store"


# --- vitals follow the vitals rule ------------------------------------------------------------------

def _vital(db, patient, caregiver, kind, value, unit, hours_ago):
    row = PatientVital(patient_id=patient.id, recorded_by_user_id=caregiver.user_id, vital_type=kind, unit=unit,
                       recorded_at=datetime.utcnow() - timedelta(hours=hours_ago))
    row.value = value
    db.add(row)
    db.commit()


def test_latest_vitals_are_the_newest_of_each_type_during_an_in_progress_visit(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    _vital(db, patient, caregiver, "heart_rate", "70", "bpm", 30)
    _vital(db, patient, caregiver, "heart_rate", "84", "bpm", 1)
    _vital(db, patient, caregiver, "blood_pressure", "138/85", "mmHg", 2)
    booking = _booking(db, patient, caregiver, BookingStatus.IN_PROGRESS)
    vitals = _get(client, booking, caregiver).json()["latest_vitals"]
    assert {v["vital_type"]: (v["value"], v["unit"]) for v in vitals} == {
        "heart_rate": ("84", "bpm"), "blood_pressure": ("138/85", "mmHg")}
    assert all(set(v) == {"vital_type", "value", "unit", "recorded_at"} for v in vitals)
    assert "status" not in vitals[0]  # no clinical rating: thresholds are not signed off


def test_latest_vitals_is_an_empty_list_when_none_were_recorded_and_null_when_not_available(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    live = _booking(db, patient, caregiver, BookingStatus.IN_PROGRESS)
    assert _get(client, live, caregiver).json()["latest_vitals"] == []

    patient2, caregiver2 = _patient(db), make_caregiver(db)
    _vital(db, patient2, caregiver2, "heart_rate", "84", "bpm", 1)
    accepted = _booking(db, patient2, caregiver2, BookingStatus.ACCEPTED)
    body = _get(client, accepted, caregiver2).json()
    assert body["latest_vitals"] is None  # not allowed before the visit starts
    assert "84" not in str(body)


def test_a_patient_grant_makes_vitals_available_before_the_visit_starts(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    _vital(db, patient, caregiver, "heart_rate", "84", "bpm", 1)
    db.add(PatientCaregiverAccess(patient_id=patient.id, caregiver_id=caregiver.id))
    db.commit()
    booking = _booking(db, patient, caregiver, BookingStatus.ACCEPTED)
    assert [v["value"] for v in _get(client, booking, caregiver).json()["latest_vitals"]] == ["84"]


# --- a visit booked for someone else (WhatsApp) -----------------------------------------------------

def _recipient(db, creator, **over):
    fields = dict(full_name="Mama Recipient", date_of_birth=_years_ago(82), gender="female",
                  address="3 Recipient Road", created_by_user_id=creator.user_id)
    fields.update(over)
    rec = CareRecipient(**fields)
    db.add(rec)
    db.commit()
    return rec


def test_a_visit_booked_for_someone_else_shows_that_person_not_the_booker(db, client):
    booker = _patient(db, full_name="Bola Booker", phone_number="+2348000000777", date_of_birth=_years_ago(45),
                      gender="male", blood_group="A+", allergies="Booker allergy", medications="Booker meds",
                      emergency_contact_name="Booker Kin", emergency_contact_phone="+2348000000666")
    caregiver = make_caregiver(db)
    recipient = _recipient(db, booker)
    booking = _booking(db, booker, caregiver, BookingStatus.IN_PROGRESS, booking_for="mother",
                       channel="whatsapp", booked_by_user_id=booker.user_id, care_recipient_id=recipient.id)
    r = _get(client, booking, caregiver)
    body = r.json()
    assert body["full_name"] == "Mama Recipient" and body["age"] == 82 and body["gender"] == "female"
    assert body["booking_for"] == "mother"
    # The booker's own health data is not the client's.
    assert body["medical_history"] is None
    for secret in ("Booker allergy", "Booker meds", "A+", "Booker Kin", "+2348000000666", "3 Recipient Road"):
        assert secret not in r.text
    # Whoever booked is the family contact.
    assert body["emergency_contact"] == {"name": "Bola Booker", "phone": "+2348000000777", "relationship": "mother"}


def test_a_visit_booked_for_oneself_through_whatsapp_uses_the_patient_record(db, client):
    patient, caregiver = _patient(db), make_caregiver(db)
    recipient = _recipient(db, patient, full_name="Ada Patient", gender="female")
    booking = _booking(db, patient, caregiver, booking_for="myself", channel="whatsapp",
                       booked_by_user_id=patient.user_id, care_recipient_id=recipient.id)
    body = _get(client, booking, caregiver).json()
    assert body["full_name"] == "Ada Patient" and body["emergency_contact"]["name"] == "Next of Kin"
