"""Shared pieces for the care-alert tests (slice 1c): fake Resend and Expo, a consenting family contact, plans built straight in the
database with a date of our choosing, and a reading saved through the real API."""

import html
import json
from datetime import date, datetime, time, timedelta

import httpx
import pytest

from app.core.config import settings
from app.core.security import encrypt
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.care_plan import CarePlan, CarePlanTask, CarePlanTaskCompletion
from app.db.models.consent import Consent
from app.db.models.push_token import PushToken
from app.services import admin_notify, care_alert_delivery
from app.services import email as email_service
from app.tests.factories import auth_headers, make_patient, make_user

VITALS = "/api/v1/vitals/"
ADMIN_ALERTS = "/api/v1/admin/care-alerts"


class Outbox:
    """Every request the fake Resend saw."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"id": "email-id-placeholder"})

    @property
    def mails(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def to(self) -> list[str]:
        return sorted(m["to"][0] for m in self.mails)

    def body(self, to: str) -> str:
        """The mail's HTML with the entities undone, so an apostrophe reads as an apostrophe."""
        return html.unescape(next(m["html"] for m in self.mails if m["to"][0] == to))


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(box.handler))
    monkeypatch.setattr(settings, "RESEND_KEY", "re_placeholder_not_a_secret")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setattr(settings, "EMAILS_FROM_NAME", "Oakly")
    monkeypatch.setattr(settings, "ADMIN_NOTIFY_EMAIL", "")
    monkeypatch.setattr(settings, "ADMIN_CONSOLE_URL", "https://admin.oaklycare.com")
    monkeypatch.setattr(admin_notify, "_send_inline", True)
    monkeypatch.setattr(care_alert_delivery, "_send_inline", True)
    return box


def give_consent(db, recipient, decision="granted"):
    db.add(Consent(subject_recipient_id=recipient.id, type="share_with_family", decision=decision, policy_version="test",
                   channel="test"))
    db.commit()


def self_recipient(db, patient) -> CareRecipient:
    """The recipient row that stands for an app or WhatsApp patient themselves (role self), as the WhatsApp channel creates it."""
    recipient = CareRecipient(full_name=patient.full_name, created_by_user_id=patient.user_id)
    db.add(recipient)
    db.commit()
    db.add(CareRelationship(contact_user_id=patient.user_id, recipient_id=recipient.id, role="self", clinical_access=True))
    db.commit()
    return recipient


def add_family(db, recipient, *, role="family", clinical_access=True, verified=True, email=True, push=False, active=True,
               name="Family Member"):
    user = make_user(db, "patient", full_name=name, is_verified=verified, is_active=active, **({} if email else {"email": None}))
    db.add(CareRelationship(contact_user_id=user.id, recipient_id=recipient.id, role=role, clinical_access=clinical_access))
    if push:
        db.add(PushToken(user_id=user.id, token=f"ExponentPushToken[{user.id.hex[:12]}]", platform="android"))
    db.commit()
    return user


def patient_with_family(db, *, first_name="Ada", consent=True, **family):
    """A patient who has a self recipient and one family contact. Returns (patient, recipient, family user)."""
    patient = make_patient(db, full_name=f"{first_name} Okafor")
    recipient = self_recipient(db, patient)
    if consent:
        give_consent(db, recipient)
    return patient, recipient, add_family(db, recipient, **family)


def post_reading(client, patient, vital_type, value, unit, notes=None, recorded_at=None):
    payload = {"patient_id": str(patient.id), "recorded_by_user_id": str(patient.user_id), "vital_type": vital_type,
               "value": value, "unit": unit, "source": "manual"}
    if notes is not None:
        payload["notes"] = notes
    if recorded_at is not None:
        payload["recorded_at"] = recorded_at.isoformat()
    return client.post(VITALS, json=payload, headers=auth_headers(patient.user))


# --- plans and ticks, built in the database so a test can pick the date -------------------------------------------------

def make_plan(db, caregiver, *, patient=None, recipient=None, start=date(2026, 11, 1), days=60, status="active") -> CarePlan:
    plan = CarePlan(patient_id=patient.id if patient else None, care_recipient_id=recipient.id if recipient else None,
                    risk_level="medium", diagnosis_encrypted=encrypt("Placeholder diagnosis"), start_date=start,
                    duration_days=days, created_by_caregiver_id=caregiver.id, status=status)
    db.add(plan)
    db.commit()
    return plan


def add_task(db, plan, title, slot="morning", days=(1, 2, 3, 4, 5, 6, 7), created=datetime(2026, 11, 1, 8, 0), active=True):
    task = CarePlanTask(plan_id=plan.id, title_encrypted=encrypt(title), time_slot=slot, time_of_day=time(8, 0),
                        days_of_week=list(days), is_active=active, created_at=created, updated_at=created)
    db.add(task)
    db.commit()
    return task


def tick_task(db, task, day, caregiver, booking):
    db.add(CarePlanTaskCompletion(task_id=task.id, for_date=day, completed_by_caregiver_id=caregiver.id, booking_id=booking.id,
                                  completed_at=datetime.utcnow()))
    db.commit()


# A Tuesday in Lagos time (UTC+1): a morning visit 08:00 to 12:00 local is 07:00 to 11:00 UTC.
VISIT_DAY = date(2026, 11, 24)
VISIT_START_UTC = datetime(2026, 11, 24, 7, 0)
VISIT_END_UTC = datetime(2026, 11, 24, 11, 0)
