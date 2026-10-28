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


# --- slice B2: ticks, notes, progress ---------------------------------------------------------------------------------

EVERY_DAY = [1, 2, 3, 4, 5, 6, 7]
SECRET_NOTE = "Placeholder note eta"


def today_iso() -> str:
    from app.services.care_plans import local_today

    return local_today().isoformat()


def daily_task(title=SECRET_TASK, slot="morning", time="08:00", days=None):
    return {"title": title, "time_slot": slot, "time_of_day": time, "days_of_week": days or EVERY_DAY}


def started_plan(client, db, tasks=None, **over):
    """A nurse with an in-progress visit and a plan that starts today, with daily tasks (so no test depends on the weekday).
    Returns (nurse, patient, booking, plan)."""
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    body = {"start_date": today_iso(), "tasks": tasks if tasks is not None else [daily_task(), daily_task("Placeholder task two", "evening", "18:00")]}
    body.update(over)
    r = create_plan(client, nurse, booking, **body)
    assert r.status_code == 201, r.text
    return nurse, patient, booking, r.json()


def tick(client, caregiver, task_id, done=True, day=None):
    return client.put(f"{BASE}/care-plan-tasks/{task_id}/completion", json={"date": day or today_iso(), "done": done},
                      headers=h(caregiver))


def colleague(db, patient, status=BookingStatus.IN_PROGRESS, nurse=False):
    """Another caregiver with a visit for the same client."""
    other = make_nurse(db) if nurse else make_caregiver(db)
    make_booking(db, patient, other, status=status, booking_for="myself")
    return other


# --- slice B3: medications ----------------------------------------------------------------------------------------------

SECRET_MED = "Placeholder medicine theta"
SECRET_MED_NOTE = "Placeholder medicine note iota"
SECRET_MED_REASON = "Placeholder medicine reason kappa"
SECRET_MED_PRESCRIBER = "Placeholder prescriber lambda"
SECRET_MED_TEXTS = (SECRET_MED, SECRET_MED_NOTE, SECRET_MED_REASON, SECRET_MED_PRESCRIBER)


def med_body(**over):
    body = {"name": SECRET_MED, "strength": "500 mg", "dose_amount": "1", "dose_unit": "tablet", "route": "oral",
            "reason": SECRET_MED_REASON, "prescribed_by": SECRET_MED_PRESCRIBER, "note": SECRET_MED_NOTE,
            "frequency": "twice_daily", "times_of_day": ["20:00", "08:00"], "start_date": "2026-10-28", "end_date": "2026-11-28"}
    body.update(over)
    return body


def add_med(client, caregiver, plan_id, **over):
    return client.post(f"{BASE}/care-plans/{plan_id}/medications", json=med_body(**over), headers=h(caregiver))
