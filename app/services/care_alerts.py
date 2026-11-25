"""Care alerts (slice 1c): raise, debounce and work an alert. The clinical lead asked for an alert when a vital reading is out of
range and when a care-plan task is missed (clinician pack sections 3 and 5); the owner decided on 2026-11-25 that it goes to the
family contact and to the admin queue.

The rules, in one place (the SOP ``docs/sop/2026-11-25-care-alerts.md`` repeats them):

* **Out-of-range reading.** After a reading is saved (create, or an edit of its type, value or unit) the vitals flags engine rates it
  (``vital_flags.reading_flag``; never re-implemented here). High and low raise a ``warning`` alert, critical raises a ``critical``
  one (an emergency: "call 112"). Weight, sleep, unreadable values and Normal raise nothing. A reading recorded more than
  ``CARE_ALERT_VITAL_MAX_AGE_MINUTES`` ago (a back-filled entry) raises nothing.
* **Missed task.** See ``care_alert_missed_tasks``: a task that was due in a completed visit and has no tick for that day when the visit ended.
* **Debounce, per patient + type + severity.** While an alert of the same patient, type, severity (and care recipient) is still open or
  acknowledged and was last fed inside the window, a new occurrence only bumps its ``occurrence_count`` and ``last_occurred_at``: no new
  row and no new message. Windows: vital warning ``CARE_ALERT_VITAL_DEBOUNCE_MINUTES`` (240), vital critical
  ``CARE_ALERT_CRITICAL_DEBOUNCE_MINUTES`` (30, so a persisting emergency is repeated), missed task
  ``CARE_ALERT_TASK_DEBOUNCE_MINUTES`` (720). Severity is part of the key so a critical reading is never swallowed by an earlier
  warning, and a resolved alert never absorbs anything (resolving means a person dealt with it; the next occurrence is news).
* **Idempotent.** Every occurrence carries a ``source_key`` (``vital:<reading>:<severity>`` or ``visit:<booking>``), unique, so the same
  reading or visit can be evaluated twice (a retry, a re-run of the sweep) without a second alert or a double count.

Delivery (family e-mail and push, admin e-mail for critical) is in ``care_alert_delivery``. ``on_vital_recorded`` never raises: it runs
after the caregiver's request has been committed, like ``admin_notify``.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt, encrypt
from app.db.models.admin import AdminUser
from app.db.models.care_alert import CareAlert, CareAlertOccurrence
from app.db.models.patient_vital import PatientVital
from app.db.session import SessionLocal
from app.services import vital_flags

logger = logging.getLogger(__name__)

VITAL_ALERT = "vital_out_of_range"
TASK_ALERT = "task_missed"
WARNING, CRITICAL = "warning", "critical"
OPEN, ACKNOWLEDGED, RESOLVED = "open", "acknowledged", "resolved"
ACTIVE = (OPEN, ACKNOWLEDGED)

VITAL_LABELS = {"heart_rate": "heart rate", "blood_pressure": "blood pressure", "temperature": "temperature",
                "respiratory_rate": "breathing rate", "blood_glucose": "blood glucose"}


class AlertError(Exception):
    """A refused admin action. The endpoint answers 409 ``{"detail": {"code": ..., "message": ...}}``."""
    code = "error"
    message = "Error"


class AlreadyAcknowledged(AlertError):
    code = "already_acknowledged"
    message = "This alert has already been acknowledged."


class AlreadyResolved(AlertError):
    code = "already_resolved"
    message = "This alert has already been resolved."


@dataclass
class Raised:
    alert_id: UUID
    created: bool  # False: absorbed into an alert that was already open (no message goes out)


def debounce_window(alert_type: str, severity: str) -> timedelta:
    if alert_type == TASK_ALERT:
        return timedelta(minutes=settings.CARE_ALERT_TASK_DEBOUNCE_MINUTES)
    if severity == CRITICAL:
        return timedelta(minutes=settings.CARE_ALERT_CRITICAL_DEBOUNCE_MINUTES)
    return timedelta(minutes=settings.CARE_ALERT_VITAL_DEBOUNCE_MINUTES)


def seal_detail(detail: Dict[str, Any]) -> str:
    return encrypt(json.dumps(detail))


def open_detail(alert: CareAlert) -> Optional[Dict[str, Any]]:
    if not alert.detail_encrypted:
        return None
    try:
        return json.loads(decrypt(alert.detail_encrypted))
    except Exception:  # noqa: BLE001 - an unreadable detail must not break a queue listing
        logger.error("care alert detail unreadable alert=%s", alert.id)
        return None


def raise_alert(db: Session, *, alert_type: str, severity: str, patient_id: UUID, source_key: str, summary: str,
                detail: Dict[str, Any], now: datetime, care_recipient_id: Optional[UUID] = None,
                booking_id: Optional[UUID] = None, vital_id: Optional[UUID] = None) -> Optional[Raised]:
    """Record one occurrence, creating the alert or absorbing it into the open one (the debounce above). Returns None when this
    ``source_key`` was already recorded. Flushes only: the caller commits."""
    # One writer per patient + type + severity, so two readings saved at once cannot each open an alert.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
               {"k": f"care_alert:{patient_id}:{alert_type}:{severity}"})
    if db.scalar(select(CareAlertOccurrence.id).where(CareAlertOccurrence.source_key == source_key)) is not None:
        return None
    recent = db.scalars(
        select(CareAlert).where(
            CareAlert.patient_id == patient_id, CareAlert.type == alert_type, CareAlert.severity == severity,
            CareAlert.care_recipient_id.is_not_distinct_from(care_recipient_id),
            CareAlert.status.in_(ACTIVE), CareAlert.last_occurred_at >= now - debounce_window(alert_type, severity))
        .order_by(CareAlert.last_occurred_at.desc()).limit(1).with_for_update()).first()
    sealed = seal_detail(detail)
    if recent is not None:
        recent.occurrence_count += 1
        recent.last_occurred_at = now
        recent.detail_encrypted = sealed
        recent.summary = summary
        recent.booking_id = booking_id or recent.booking_id
        recent.vital_id = vital_id or recent.vital_id
        db.add(CareAlertOccurrence(alert_id=recent.id, source_key=source_key))
        db.flush()
        return Raised(recent.id, created=False)
    alert = CareAlert(type=alert_type, severity=severity, status=OPEN, patient_id=patient_id, care_recipient_id=care_recipient_id,
                      booking_id=booking_id, vital_id=vital_id, summary=summary, detail_encrypted=sealed, occurrence_count=1,
                      last_occurred_at=now, created_at=now, updated_at=now)
    db.add(alert)
    db.flush()
    db.add(CareAlertOccurrence(alert_id=alert.id, source_key=source_key))
    db.flush()
    return Raised(alert.id, created=True)


# --- readings ----------------------------------------------------------------------------------------------------------

def vital_summary(vital_type: str, severity: str, status: str) -> str:
    label = VITAL_LABELS.get(vital_type, "vital sign").capitalize()
    if severity == CRITICAL:
        return f"{label} reading is at a critical level."
    return f"{label} reading is {'higher' if status == vital_flags.HIGH else 'lower'} than the safe range."


def alert_for_reading(db: Session, vital: PatientVital, now: datetime) -> Optional[Raised]:
    """Rate a stored reading and raise (or feed) its alert. None when nothing is to be raised. Flushes only."""
    recorded = vital.recorded_at or now
    if recorded < now - timedelta(minutes=settings.CARE_ALERT_VITAL_MAX_AGE_MINUTES):
        return None
    flag = vital_flags.reading_flag(vital, vital_flags.patient_context(db, vital.patient_id))
    if not vital_flags.is_out_of_range(flag):
        return None
    severity = CRITICAL if flag.status == vital_flags.CRITICAL else WARNING
    detail = {"vital_type": vital.vital_type, "status": flag.status, "reason": flag.reason, "value": vital.value, "unit": vital.unit}
    return raise_alert(
        db, alert_type=VITAL_ALERT, severity=severity, patient_id=vital.patient_id, source_key=f"vital:{vital.id}:{severity}",
        summary=vital_summary(vital.vital_type, severity, flag.status), detail=detail, now=now, vital_id=vital.id)


def on_vital_recorded(vital_id: UUID) -> None:
    """Called after a reading was saved and committed. Never raises: a failure is logged and the caregiver's request is untouched."""
    try:
        from app.services import care_alert_delivery

        now = datetime.utcnow()
        with SessionLocal() as db:
            vital = db.get(PatientVital, vital_id)
            if vital is None:
                return
            raised = alert_for_reading(db, vital, now)
            db.commit()
        if raised is not None and raised.created:
            care_alert_delivery.deliver(raised.alert_id)
    except Exception:  # noqa: BLE001 - best effort, see the docstring
        logger.warning("care alert for reading failed vital=%s", vital_id, exc_info=True)


# --- the admin's side --------------------------------------------------------------------------------------------------

def _locked(db: Session, alert_id: UUID) -> Optional[CareAlert]:
    return db.scalars(select(CareAlert).where(CareAlert.id == alert_id).with_for_update()
                      .execution_options(populate_existing=True)).first()


def acknowledge(db: Session, alert: CareAlert, admin: AdminUser, note: Optional[str], now: datetime) -> str:
    """open -> acknowledged. Returns the previous status. Raises ``AlreadyAcknowledged`` / ``AlreadyResolved``. Flushes only."""
    alert = _locked(db, alert.id)
    if alert.status == RESOLVED:
        raise AlreadyResolved()
    if alert.status == ACKNOWLEDGED:
        raise AlreadyAcknowledged()
    alert.status = ACKNOWLEDGED
    alert.acknowledged_by_admin_id = admin.id
    alert.acknowledged_at = now
    if note:
        alert.note_encrypted = encrypt(note)
    db.flush()
    return OPEN


def resolve(db: Session, alert: CareAlert, admin: AdminUser, note: Optional[str], now: datetime) -> str:
    """open or acknowledged -> resolved (an alert may be closed without being acknowledged first). Returns the previous status."""
    alert = _locked(db, alert.id)
    if alert.status == RESOLVED:
        raise AlreadyResolved()
    previous = alert.status
    alert.status = RESOLVED
    alert.resolved_by_admin_id = admin.id
    alert.resolved_at = now
    if note:
        alert.note_encrypted = encrypt(note)
    db.flush()
    return previous


def open_note(alert: CareAlert) -> Optional[str]:
    if not alert.note_encrypted:
        return None
    try:
        return decrypt(alert.note_encrypted)
    except Exception:  # noqa: BLE001
        logger.error("care alert note unreadable alert=%s", alert.id)
        return None
