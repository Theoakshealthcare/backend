"""Shared setup for the care plan tests."""

from datetime import date, timedelta

from app.db.models.booking import BookingStatus
from app.db.models.care import CareRecipient
from app.db.models.care_plan import CarePlanTemplate
from app.db.models.caregiver import CaregiverSkill
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

BASE = "/api/v1/caregiver"
NOT_FOUND = {"detail": "Care plan not found"}
SECRET_DIAGNOSIS = "Placeholder diagnosis alpha"
SECRET_ALLERGY = "Placeholder allergen beta"
SECRET_INSTRUCTION = "Placeholder instruction gamma"
SECRET_GOAL = "Placeholder goal delta"
SECRET_TARGET = "Placeholder target epsilon"
SECRET_TASK = "Placeholder task zeta"
SECRET_TEXTS = (SECRET_DIAGNOSIS, SECRET_ALLERGY, SECRET_INSTRUCTION, SECRET_GOAL, SECRET_TARGET, SECRET_TASK)


def make_nurse(db, **overrides):
    caregiver = make_caregiver(db, **overrides)
    db.add(CaregiverSkill(caregiver_id=caregiver.id, service_type="nurse"))
    db.commit()
    return caregiver


def h(caregiver):
    return auth_headers(caregiver.user)


def self_visit(db, caregiver, status=BookingStatus.IN_PROGRESS, **over):
    """A visit booked for oneself: the patient is the client. Returns (patient, booking)."""
    patient = make_patient(db, full_name="Placeholder Client", gender="female",
                           date_of_birth=date.today() - timedelta(days=71 * 366))
    over.setdefault("booking_for", "myself")
    return patient, make_booking(db, patient, caregiver, status=status, **over)


def relative_visit(db, caregiver, status=BookingStatus.IN_PROGRESS, **over):
    """A WhatsApp visit booked for a relative: the recipient is the client. Returns (booker, recipient, booking)."""
    booker = make_patient(db, full_name="Placeholder Booker")
    recipient = CareRecipient(full_name="Placeholder Relative", gender="male", date_of_birth=date(1950, 3, 4),
                              created_by_user_id=booker.user_id)
    db.add(recipient)
    db.commit()
    booking = make_booking(db, booker, caregiver, status=status, booking_for="mother", channel="whatsapp",
                           booked_by_user_id=booker.user_id, care_recipient_id=recipient.id, **over)
    return booker, recipient, booking


def plan_body(**over):
    body = {
        "diagnosis": SECRET_DIAGNOSIS, "risk_level": "high", "start_date": "2026-10-28", "duration_days": 30,
        "review_every_days": 14,
        "goals": [{"title": SECRET_GOAL, "target": SECRET_TARGET, "frequency": "daily"}],
        "tasks": [{"title": SECRET_TASK, "time_slot": "morning", "time_of_day": "08:00", "days_of_week": [1, 3, 5]}],
        "allergies": [SECRET_ALLERGY], "special_instructions": [SECRET_INSTRUCTION],
    }
    body.update(over)
    return body


def create_plan(client, caregiver, booking, **over):
    return client.post(f"{BASE}/bookings/{booking.id}/care-plan", json=plan_body(**over), headers=h(caregiver))


def a_template(db, key="stroke_recovery") -> CarePlanTemplate:
    return db.query(CarePlanTemplate).filter(CarePlanTemplate.key == key).one()
