"""A caregiver who has only been offered a job sees the job, not the patient.

Until the caregiver is the booking's assigned caregiver, offer views (the new_requests list,
notifications, and the decline response) carry no patient name, phone, emergency contact,
exact address, condition or special instructions; the location is rounded to the area.
"""

from app.db.models.booking import BookingNotification, BookingStatus, PaymentStatus
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

SECRET_BITS = ("Ada Patient", "+2348000000999", "Next of Kin", "+2348000000888",
               "12 Secret Close", "insulin-dependent", "gate code 4321")


def _offered_booking(db, caregiver):
    patient = make_patient(db, full_name="Ada Patient", phone_number="+2348000000999",
                           emergency_contact_name="Next of Kin", emergency_contact_phone="+2348000000888")
    booking = make_booking(
        db, patient, status=BookingStatus.PENDING, payment_status=PaymentStatus.PAID,
        patient_address="12 Secret Close, Lekki Phase 1", patient_latitude=6.447412,
        patient_longitude=3.472311, condition="insulin-dependent diabetes",
        special_instructions="gate code 4321",
    )
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="New visit request",
                               message="New nurse visit in Lagos", notification_type="new_booking"))
    db.commit()
    return booking


def _assert_redacted(item: dict):
    flat = str(item)
    for secret in SECRET_BITS:
        assert secret not in flat, f"offer view leaks {secret!r}"
    assert item.get("patient_details") is None
    assert item["condition"] is None and item["special_instructions"] is None
    assert item["patient_address"] == "Lagos, Lagos"  # area only
    assert item["patient_latitude"] == 6.45 and item["patient_longitude"] == 3.47  # ~1 km


def test_new_requests_list_hides_patient_details(db, client):
    caregiver = make_caregiver(db)
    _offered_booking(db, caregiver)
    r = client.get("/api/v1/booking/caregiver/bookings", params={"status": "new_requests"},
                   headers=auth_headers(caregiver.user))
    assert r.status_code == 200 and len(r.json()) == 1
    _assert_redacted(r.json()[0])


def test_notifications_hide_patient_details(db, client):
    caregiver = make_caregiver(db)
    _offered_booking(db, caregiver)
    r = client.get("/api/v1/booking/caregiver/notifications", headers=auth_headers(caregiver.user))
    assert r.status_code == 200 and len(r.json()) == 1
    _assert_redacted(r.json()[0]["booking"])


def test_decline_response_hides_patient_details(db, client):
    caregiver = make_caregiver(db)
    booking = _offered_booking(db, caregiver)
    r = client.post(f"/api/v1/booking/caregiver/action/{booking.id}", json={"action": "decline"},
                    headers=auth_headers(caregiver.user))
    assert r.status_code == 200
    _assert_redacted(r.json()["booking"])


def test_assigned_caregiver_sees_full_details_after_accepting(db, client):
    caregiver = make_caregiver(db)
    booking = _offered_booking(db, caregiver)
    r = client.post(f"/api/v1/booking/caregiver/action/{booking.id}", json={"action": "accept"},
                    headers=auth_headers(caregiver.user))
    assert r.status_code == 200
    item = r.json()["booking"]
    assert item["patient_details"]["full_name"] == "Ada Patient"
    assert item["patient_address"] == "12 Secret Close, Lekki Phase 1"
    assert item["condition"] == "insulin-dependent diabetes"

    # and in their list of accepted bookings
    r = client.get("/api/v1/booking/caregiver/bookings", params={"status": "accepted"},
                   headers=auth_headers(caregiver.user))
    assert r.json()[0]["patient_details"]["phone_number"] == "+2348000000999"


def test_other_offered_caregiver_still_redacted_after_someone_else_accepts(db, client):
    winner, other = make_caregiver(db), make_caregiver(db)
    booking = _offered_booking(db, winner)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=other.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    assert client.post(f"/api/v1/booking/caregiver/action/{booking.id}", json={"action": "accept"},
                       headers=auth_headers(winner.user)).status_code == 200
    r = client.get("/api/v1/booking/caregiver/notifications", headers=auth_headers(other.user))
    _assert_redacted(r.json()[0]["booking"])
