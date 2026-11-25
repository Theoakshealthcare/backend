"""Telling people about a NEW care alert (slice 1c): the family contact, and for a critical alert the admin e-mail route.

**Who counts as the family contact.** Only a person the patient allowed and whose contact we can trust, using data that already
exists (no new consent model):

* the alert's subject is a ``care_recipients`` row: the care recipient on a visit booked for a relative, or, for a patient or a
  reading, the recipient row that stands for the patient themselves (the ``care_relationships`` row of role ``self``);
* the contact has a ``care_relationships`` row on that recipient of role ``family`` or ``guardian`` with ``clinical_access`` true, AND the
  latest ``share_with_family`` consent for the recipient is *granted* (the rule ``care_plan_access.resolve_family_access`` already uses);
* the contact's account is active.

Reached by e-mail only when the address is on the account and the account is verified (``users.is_verified``), by push only when a
device token is registered (that is the app identity). A WhatsApp-only family contact has neither, so nothing is sent (WhatsApp
alerts are parked). ``patient_family_members`` (names typed into a profile) are NOT used: they carry no verification and no
consent, so they could not meet the rule above; they stay unused until the family view decides how those contacts are verified.

**What the message says.** The first name, one plain sentence, "call 112" when critical. Never a reading, a diagnosis, a task name,
a note, a voice note or a medicine. Every alert goes to the admin queue regardless of any of this.

**Best effort.** ``deliver`` never raises and runs after the alert has been committed. The network calls go to a worker thread so a
slow mail or push provider never holds the caregiver's request; that thread uses a session of its own. Only a NEW alert is delivered
(a debounced repeat is silent, see ``care_alerts``), and each alert is delivered once (``family_notified_at`` / ``admin_notified_at``
are set only after the attempt, so a crash before it leaves them empty).
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.care_alert import CareAlert
from app.db.models.patient import Patient
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.db.session import SessionLocal
from app.services import admin_notify, care_alerts
from app.services import email as email_service
from app.services.care_plan_access import FAMILY_ROLES, _latest_family_consent_granted
from app.services.push import get_push_service

logger = logging.getLogger(__name__)

FAMILY_TITLE = "Oakly care alert"
PUSH_TYPE = "care_alert"
EMERGENCY_LINE = "This may be an emergency. Call 112 now."

# Test hook: True runs the delivery on the calling thread, so a test can read the mail and push fakes straight after the call.
_send_inline = False
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="care-alert")


@dataclass
class Contact:
    user_id: UUID
    email: Optional[str]  # None unless the address is on the account and the account is verified
    push: bool  # at least one device token


def deliver(alert_id: UUID) -> None:
    """Deliver a newly created alert. Never raises."""
    try:
        if _send_inline:
            _deliver(alert_id)
        else:
            _executor.submit(_deliver_quietly, alert_id)
    except Exception:  # noqa: BLE001
        logger.warning("care alert delivery could not be queued alert=%s", alert_id, exc_info=True)


def deliver_all(alert_ids: List[UUID]) -> None:
    for alert_id in alert_ids:
        deliver(alert_id)


def _deliver_quietly(alert_id: UUID) -> None:
    try:
        _deliver(alert_id)
    except Exception:  # noqa: BLE001
        logger.warning("care alert delivery failed alert=%s", alert_id, exc_info=True)


# --- who -----------------------------------------------------------------------------------------------------------------

def subject_recipient_id(db: Session, alert: CareAlert) -> Optional[UUID]:
    """The care recipient the alert is about: the visit's recipient, or the one that stands for the patient themselves."""
    if alert.care_recipient_id is not None:
        return alert.care_recipient_id
    patient = db.get(Patient, alert.patient_id)
    if patient is None:
        return None
    return db.scalars(select(CareRelationship.recipient_id).where(
        CareRelationship.contact_user_id == patient.user_id, CareRelationship.role == "self")).first()


def family_contacts(db: Session, alert: CareAlert) -> List[Contact]:
    """The people the rule in the module docstring allows, with the channels each can be reached on."""
    recipient_id = subject_recipient_id(db, alert)
    if recipient_id is None or not _latest_family_consent_granted(db, recipient_id):
        return []
    patient = db.get(Patient, alert.patient_id)
    users = db.scalars(
        select(User).join(CareRelationship, CareRelationship.contact_user_id == User.id).where(
            CareRelationship.recipient_id == recipient_id, CareRelationship.role.in_(FAMILY_ROLES),
            CareRelationship.clinical_access.is_(True), User.is_active.is_(True)).distinct()).all()
    contacts = []
    for user in users:
        if patient is not None and user.id == patient.user_id:
            continue
        has_push = db.scalar(select(PushToken.id).where(PushToken.user_id == user.id).limit(1)) is not None
        email = user.email.strip() if user.email and user.email.strip() and user.is_verified else None
        if email or has_push:
            contacts.append(Contact(user.id, email, has_push))
    return contacts


def subject_first_name(db: Session, alert: CareAlert) -> str:
    name = None
    if alert.care_recipient_id is not None:
        recipient = db.get(CareRecipient, alert.care_recipient_id)
        name = recipient.full_name if recipient else None
    else:
        patient = db.get(Patient, alert.patient_id)
        name = patient.full_name if patient else None
    parts = (name or "").split()
    return parts[0] if parts else "your family member"


# --- what ------------------------------------------------------------------------------------------------------------------

def family_sentence(alert: CareAlert, first_name: str, detail: Optional[dict]) -> str:
    """One plain sentence (plus the 112 line when critical). The caller has no reading, task or condition to leak: only the
    type, the severity and, for a reading, high or low and the kind of vital sign."""
    if alert.type == care_alerts.TASK_ALERT:
        count = len((detail or {}).get("tasks") or [])
        if count == 1:
            return f"A care task planned for {first_name}'s visit was not marked as done."
        return f"Some care tasks planned for {first_name}'s visit were not marked as done."
    label = care_alerts.VITAL_LABELS.get((detail or {}).get("vital_type"), "vital sign")
    if alert.severity == care_alerts.CRITICAL:
        return f"A {label} reading for {first_name} is at a dangerous level. {EMERGENCY_LINE}"
    direction = "higher" if (detail or {}).get("status") == "high" else "lower"
    return f"A {label} reading for {first_name} was {direction} than the safe range. Please check in with {first_name} or their doctor."


def _mark(alert_id: UUID, **fields) -> None:
    with SessionLocal() as db:
        alert = db.get(CareAlert, alert_id)
        if alert is not None:
            for key, value in fields.items():
                setattr(alert, key, value)
            db.commit()


def _deliver(alert_id: UUID) -> None:
    with SessionLocal() as db:
        alert = db.get(CareAlert, alert_id)
        if alert is None:
            return
        already_family = alert.family_notified_at is not None
        already_admin = alert.admin_notified_at is not None
        critical = alert.severity == care_alerts.CRITICAL
        sentence = None if already_family else family_sentence(alert, subject_first_name(db, alert), care_alerts.open_detail(alert))
        contacts = [] if already_family else family_contacts(db, alert)
        admin_mails = admin_notify.critical_care_alert_mails(db) if critical and not already_admin else []

    reached = 0
    if not already_family and sentence is not None:
        reached = _tell_family(contacts, sentence, critical)
        _mark(alert_id, family_notified_count=reached, family_notified_at=datetime.utcnow())
    if admin_mails:
        admin_notify.send_all(admin_mails)
        _mark(alert_id, admin_notified_at=datetime.utcnow())


def _tell_family(contacts: List[Contact], sentence: str, critical: bool) -> int:
    """E-mail and push each contact; returns how many people were reached on at least one channel. Never raises."""
    if not contacts:
        return 0
    reached = 0
    html = email_service.render_template("care_alert", {
        "project_name": settings.PROJECT_NAME, "heading": FAMILY_TITLE, "message": sentence, "critical": critical})
    subject = f"{settings.PROJECT_NAME} - {FAMILY_TITLE}"
    for contact in contacts:
        ok = False
        if contact.email:
            try:
                ok = bool(email_service.send_email(contact.email, subject, html)) or ok
            except Exception:  # noqa: BLE001
                logger.warning("care alert e-mail failed", exc_info=True)
        if contact.push:
            try:
                with SessionLocal() as db:
                    accepted = get_push_service().send_to_users(
                        db, [contact.user_id], title=FAMILY_TITLE, body=sentence,
                        data={"type": PUSH_TYPE, "severity": "critical" if critical else "warning"})
                ok = accepted > 0 or ok
            except Exception:  # noqa: BLE001
                logger.warning("care alert push failed", exc_info=True)
        reached += 1 if ok else 0
    return reached
