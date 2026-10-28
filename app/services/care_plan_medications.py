"""Care plan medications: add, edit and stop (slice B3). Record only (decision D4).

A medication is what the prescriber ordered, as the nurse wrote it down. Nothing in this module checks a dose, compares two
medicines, schedules a reminder or records that a dose was given; administration is a visit task, ticked like any other.

The descriptive fields (name, strength, dose, route, reason, prescriber, note) are one Fernet-sealed JSON document; the
frequency, times of day and dates stay plaintext. There is no plaintext fallback (see care_plans.py). A medication is stopped,
never deleted, and a stopped one is history: it cannot be edited, only a new one recorded. Every write holds the plan row
``FOR UPDATE`` like the goal and task writes, bumps the plan's ``version`` and appends an event that carries ids and field
names, never health text.
"""

from datetime import datetime
from typing import Any, Dict, List, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.care_plan import CarePlanMedication
from app.schemas.care_plan import MAX_MEDICATIONS, MedicationCreate, MedicationPatch
from app.services import care_plans as base
from app.services.care_plans import (
    Actor, CarePlanError, InvalidPlan, actor_event, local_today, seal_json, unseal_json,
)

DETAIL_COLUMN = "care_plan_medications.detail_encrypted"


class MedicationStopped(CarePlanError):
    code, message = "medication_stopped", "This medication was stopped. Record a new one instead of editing it."


def _active_count(db: Session, plan_id: UUID) -> int:
    return db.query(CarePlanMedication.id).filter(CarePlanMedication.plan_id == plan_id,
                                                  CarePlanMedication.status == "active").count()


def _locked(db: Session, medication_id: UUID) -> CarePlanMedication:
    return (db.query(CarePlanMedication).filter(CarePlanMedication.id == medication_id).populate_existing()
            .with_for_update().one())


def _check_dates(start, end) -> None:
    if end is not None and end < start:
        raise InvalidPlan("end_date cannot be before start_date")


def add_medication(db: Session, actor: Actor, plan_id: UUID, payload: MedicationCreate) -> Tuple[CarePlanMedication, int]:
    try:
        plan = base._lock_editable(db, plan_id, payload.expected_version)
        if _active_count(db, plan.id) >= MAX_MEDICATIONS:
            raise InvalidPlan(f"a plan can have at most {MAX_MEDICATIONS} active medications")
        start = payload.start_date or local_today()
        _check_dates(start, payload.end_date)
        medication = CarePlanMedication(
            plan_id=plan.id, detail_encrypted=seal_json({f: getattr(payload, f) for f in base.MEDICATION_DETAIL_FIELDS}),
            frequency=payload.frequency, times_of_day=list(payload.times_of_day), start_date=start, end_date=payload.end_date)
        db.add(medication)
        db.flush()
        actor_event(db, plan.id, actor, "med.add", {"medication_id": str(medication.id)})
        base._touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(medication)
    return medication, version


def update_medication(db: Session, actor: Actor, medication: CarePlanMedication,
                      payload: MedicationPatch) -> Tuple[CarePlanMedication, int]:
    sent = payload.model_fields_set - {"expected_version"}
    try:
        plan = base._lock_editable(db, medication.plan_id, payload.expected_version)
        medication = _locked(db, medication.id)
        if medication.status == "stopped":
            raise MedicationStopped()
        changed: List[str] = []
        detail: Dict[str, Any] = unseal_json(medication.detail_encrypted, plan.id, DETAIL_COLUMN)
        for name in base.MEDICATION_DETAIL_FIELDS:
            if name in sent:
                detail[name] = getattr(payload, name)
                changed.append(name)
        if any(name in changed for name in base.MEDICATION_DETAIL_FIELDS):
            medication.detail_encrypted = seal_json(detail)
        for name in ("frequency", "start_date", "end_date"):
            if name in sent:
                setattr(medication, name, getattr(payload, name))
                changed.append(name)
        if "times_of_day" in sent:
            medication.times_of_day = list(payload.times_of_day)
            changed.append("times_of_day")
        _check_dates(medication.start_date, medication.end_date)
        if changed:
            medication.updated_at = datetime.utcnow()
            actor_event(db, plan.id, actor, "med.update",
                         {"medication_id": str(medication.id), "fields": changed})
            base._touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(medication)
    return medication, version


def stop_medication(db: Session, actor: Actor, medication: CarePlanMedication,
                    expected_version: int) -> Tuple[CarePlanMedication, int]:
    """Stopping keeps the row, so the plan still shows what the client was on. Stopping a stopped one changes nothing."""
    try:
        plan = base._lock_editable(db, medication.plan_id, expected_version)
        medication = _locked(db, medication.id)
        if medication.status != "stopped":
            now = datetime.utcnow()
            medication.status, medication.stopped_at, medication.updated_at = "stopped", now, now
            actor_event(db, plan.id, actor, "med.stop", {"medication_id": str(medication.id)})
            base._touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(medication)
    return medication, version


def medication_out(medication: CarePlanMedication) -> Dict[str, Any]:
    return base._medication_out(medication)
