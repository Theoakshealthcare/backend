"""Care plans for the caregiver app: create, read, edit and the goals (slice B1). Task definitions' edits, ticks, notes and
progress are in care_plan_tasks.py and care_plan_observations.py (slice B2).

Health text (diagnosis, allergies, special instructions, goal and task text) is sealed with Fernet before it is stored and
opened only when a response is built. There is deliberately no plaintext fallback: a value that cannot be decrypted is a
server error (a log line names the plan and the column, never the value), not a silent blank. Events carry field names,
ids and counts only. Services commit; they roll back and re-raise on any failure.
"""

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from cryptography.fernet import InvalidToken
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt, encrypt
from app.db.models.booking import BookingStatus
from app.db.models.care_plan import (
    CarePlan, CarePlanEvent, CarePlanGoal, CarePlanMedication, CarePlanObservation, CarePlanTask, CarePlanTemplate,
)
from app.db.models.admin import AdminUser
from app.db.models.caregiver import Caregiver
from app.schemas.care_plan import MAX_GOALS, MAX_TASKS, CarePlanCreate, CarePlanPatch, GoalCreate, GoalPatch
from app.services.care_plan_access import (
    PlanAccess, Subject, accessible_subjects, is_care_plan_author,
)
from app.services.caregiver_clients import age_in_years

logger = logging.getLogger(__name__)

DEFAULT_DURATION_DAYS = 30
LIST_LIMIT = 100


class CarePlanError(Exception):
    """Base for the refusals the endpoints map to 409/404/422. ``code`` is the machine-readable ``detail.code``."""
    code = "error"
    message = "Error"


class ClientNotIdentified(CarePlanError):
    code, message = "client_not_identified", "We can't tell who this visit is for, so a care plan isn't available."


class CarePlanExists(CarePlanError):
    code, message = "care_plan_exists", "This client already has an active care plan."

    def __init__(self, plan_id: UUID):
        super().__init__(str(plan_id))
        self.plan_id = plan_id


class VersionConflict(CarePlanError):
    code, message = "version_conflict", "Someone just changed this plan. Reload it and try again."


class PlanNotEditable(CarePlanError):
    code, message = "plan_not_editable", "This care plan is completed or archived and can no longer be changed."


class VisitNotInProgress(CarePlanError):
    code, message = "visit_not_in_progress", "Available during an active visit."


class TaskDateNotToday(CarePlanError):
    code, message = "task_date_not_today", "Tasks can only be ticked for today."


class TaskNotScheduled(CarePlanError):
    code, message = "task_not_scheduled", "This task is not scheduled for that day."


class TaskRemoved(CarePlanError):
    """The task was removed from the plan. The endpoints answer the plain 404, like any other unavailable plan part."""
    code, message = "task_removed", "Care plan not found"


class TemplateNotFound(CarePlanError):
    code, message = "template_not_found", "Care plan template not found."


class InvalidPlan(CarePlanError):
    """A request that is well-formed but cannot make a plan (422). ``message`` names the field, never the value."""
    code = "invalid_care_plan"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class CarePlanDecryptionError(RuntimeError):
    """A stored value could not be decrypted with any configured key. A server error, never a blank."""


# --- encryption helpers ---------------------------------------------------------------------------------------------

def seal(value: Optional[str]) -> Optional[str]:
    return None if value is None else encrypt(value)


def seal_list(values: Optional[List[str]]) -> Optional[str]:
    return encrypt(json.dumps(values)) if values else None


def unseal(value: Optional[str], plan_id: UUID, column: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return decrypt(value)
    except InvalidToken:
        logger.error("care plan decrypt failed plan=%s column=%s", plan_id, column)
        raise CarePlanDecryptionError(f"cannot decrypt care_plans/{plan_id} {column}") from None


def seal_json(value: Dict[str, Any]) -> str:
    return encrypt(json.dumps(value))


def unseal_json(value: Optional[str], plan_id: UUID, column: str) -> Dict[str, Any]:
    text = unseal(value, plan_id, column)
    return json.loads(text) if text else {}


def unseal_list(value: Optional[str], plan_id: UUID, column: str) -> List[str]:
    text = unseal(value, plan_id, column)
    return json.loads(text) if text else []


def local_today() -> date:
    return datetime.now(ZoneInfo(settings.HOME_TIMEZONE)).date()


# --- building responses ---------------------------------------------------------------------------------------------

def _client(subject: Subject) -> Dict[str, Any]:
    return {"full_name": subject.full_name, "age": age_in_years(subject.date_of_birth), "gender": subject.gender}


def _review_due(plan: CarePlan) -> Optional[date]:
    return plan.start_date + timedelta(days=plan.review_every_days) if plan.review_every_days else None


def _hhmm(value: Optional[time]) -> Optional[str]:
    return value.strftime("%H:%M") if value is not None else None


def _goal_out(goal: CarePlanGoal) -> Dict[str, Any]:
    return {"id": goal.id, "title": unseal(goal.title_encrypted, goal.plan_id, "care_plan_goals.title_encrypted") or "",
            "target": unseal(goal.target_encrypted, goal.plan_id, "care_plan_goals.target_encrypted"),
            "frequency": goal.frequency, "status": goal.status}


def _task_out(task: CarePlanTask) -> Dict[str, Any]:
    return {"id": task.id, "title": unseal(task.title_encrypted, task.plan_id, "care_plan_tasks.title_encrypted") or "",
            "time_slot": task.time_slot, "time_of_day": _hhmm(task.time_of_day), "days_of_week": list(task.days_of_week)}


MEDICATION_DETAIL_FIELDS = ("name", "strength", "dose_amount", "dose_unit", "route", "reason", "prescribed_by", "note")


def _medication_out(med: CarePlanMedication) -> Dict[str, Any]:
    detail = unseal_json(med.detail_encrypted, med.plan_id, "care_plan_medications.detail_encrypted")
    return {"id": med.id, **{f: detail.get(f) for f in MEDICATION_DETAIL_FIELDS}, "name": detail.get("name") or "",
            "frequency": med.frequency, "times_of_day": list(med.times_of_day or []), "start_date": med.start_date,
            "end_date": med.end_date, "status": med.status, "stopped_at": med.stopped_at}


def _medications(db: Session, plan_id: UUID) -> List[CarePlanMedication]:
    """Active first, then stopped (history), each in the order they were recorded."""
    return (db.query(CarePlanMedication).filter(CarePlanMedication.plan_id == plan_id)
            .order_by(CarePlanMedication.status, CarePlanMedication.created_at, CarePlanMedication.id).all())


def _goals(db: Session, plan_id: UUID) -> List[CarePlanGoal]:
    return (db.query(CarePlanGoal).filter(CarePlanGoal.plan_id == plan_id)
            .order_by(CarePlanGoal.sort_order, CarePlanGoal.created_at).all())


def _tasks(db: Session, plan_id: UUID) -> List[CarePlanTask]:
    return (db.query(CarePlanTask).filter(CarePlanTask.plan_id == plan_id, CarePlanTask.is_active.is_(True))
            .order_by(CarePlanTask.sort_order, CarePlanTask.created_at).all())


def _author(db: Session, plan: CarePlan) -> Dict[str, Any]:
    if plan.created_by_caregiver_id is not None:
        cg = db.get(Caregiver, plan.created_by_caregiver_id)
        return {"name": cg.full_name if cg else "Caregiver", "kind": "caregiver"}
    admin = db.get(AdminUser, plan.created_by_admin_id)
    return {"name": admin.full_name if admin else "Clinical Reviewer", "kind": "admin"}


def can_edit(db: Session, caregiver: Caregiver, plan: CarePlan) -> bool:
    return plan.status == "active" and is_care_plan_author(db, caregiver)


def can_record_visit(access: PlanAccess, plan: CarePlan) -> bool:
    """Ticks and notes: the plan is active and the caller has a visit for this client in progress (``access`` is the nearest
    live booking, and an in-progress one sorts first)."""
    return plan.status == "active" and access.booking.status == BookingStatus.IN_PROGRESS


def build_plan(db: Session, caregiver: Caregiver, access: PlanAccess, plan: CarePlan) -> Dict[str, Any]:
    template = db.get(CarePlanTemplate, plan.template_id) if plan.template_id else None
    return {
        "id": plan.id, "version": plan.version, "status": plan.status, "booking_id": access.booking.id,
        "client": _client(access.require_subject()),
        "diagnosis": unseal(plan.diagnosis_encrypted, plan.id, "care_plans.diagnosis_encrypted") or "",
        "risk_level": plan.risk_level, "start_date": plan.start_date, "duration_days": plan.duration_days,
        "review_every_days": plan.review_every_days, "review_due_on": _review_due(plan),
        "template": ({"id": template.id, "name": template.name, "version": plan.template_version or template.version}
                     if template else None),
        "author": _author(db, plan),
        "allergies": unseal_list(plan.allergies_encrypted, plan.id, "care_plans.allergies_encrypted"),
        "special_instructions": unseal_list(plan.special_instructions_encrypted, plan.id,
                                            "care_plans.special_instructions_encrypted"),
        "goals": [_goal_out(g) for g in _goals(db, plan.id)],
        "medications": [_medication_out(m) for m in _medications(db, plan.id)],
        "tasks": [_task_out(t) for t in _tasks(db, plan.id)],
        "capabilities": {"can_edit_plan": can_edit(db, caregiver, plan), "can_tick_tasks": can_record_visit(access, plan),
                         "can_add_observation": can_record_visit(access, plan)},
        "created_at": plan.created_at, "updated_at": plan.updated_at,
    }


def _summary(db: Session, plan: CarePlan) -> Dict[str, Any]:
    goals = db.query(CarePlanGoal.id).filter(CarePlanGoal.plan_id == plan.id, CarePlanGoal.status != "discontinued").count()
    tasks = db.query(CarePlanTask.id).filter(CarePlanTask.plan_id == plan.id, CarePlanTask.is_active.is_(True)).count()
    return {
        "id": plan.id, "version": plan.version, "status": plan.status,
        "diagnosis": unseal(plan.diagnosis_encrypted, plan.id, "care_plans.diagnosis_encrypted") or "",
        "risk_level": plan.risk_level, "start_date": plan.start_date, "duration_days": plan.duration_days,
        "review_every_days": plan.review_every_days, "review_due_on": _review_due(plan),
        "goal_count": goals, "task_count": tasks,
    }


def _active_plan(db: Session, subject: Subject) -> Optional[CarePlan]:
    column = CarePlan.patient_id if subject.kind == "patient" else CarePlan.care_recipient_id
    return db.query(CarePlan).filter(column == subject.id, CarePlan.status == "active").first()


def visit_summary(db: Session, caregiver: Caregiver, access: PlanAccess) -> Dict[str, Any]:
    ready = access.subject is not None
    plan = _active_plan(db, access.subject) if ready else None
    return {"booking_id": access.booking.id, "subject_ready": ready,
            "can_author": ready and is_care_plan_author(db, caregiver),
            "plan": _summary(db, plan) if plan is not None else None}


def list_assigned(db: Session, caregiver: Caregiver, status: str, q: Optional[str]) -> Dict[str, Any]:
    """Plans for the clients of the caller's live bookings, newest change first."""
    subjects = accessible_subjects(db, caregiver)
    patient_ids = [k[1] for k in subjects if k[0] == "patient"]
    recipient_ids = [k[1] for k in subjects if k[0] == "recipient"]
    match = []
    if patient_ids:
        match.append(CarePlan.patient_id.in_(patient_ids))
    if recipient_ids:
        match.append(CarePlan.care_recipient_id.in_(recipient_ids))
    if not match:
        return {"items": []}
    plans = db.query(CarePlan).filter(or_(*match), CarePlan.status == status).order_by(CarePlan.updated_at.desc()).all()
    needle = (q or "").strip().lower()
    items = []
    for plan in plans:
        access = subjects[("patient", plan.patient_id) if plan.patient_id else ("recipient", plan.care_recipient_id)]
        subject = access.require_subject()
        if needle and needle not in subject.full_name.lower():
            continue
        items.append({
            "id": plan.id, "booking_id": access.booking.id, "client": _client(subject),
            "diagnosis": unseal(plan.diagnosis_encrypted, plan.id, "care_plans.diagnosis_encrypted") or "",
            "risk_level": plan.risk_level, "status": plan.status, "start_date": plan.start_date,
        })
    return {"items": items[:LIST_LIMIT]}  # the name filter runs before the cap


def list_candidates(db: Session, caregiver: Caregiver) -> Dict[str, Any]:
    """Identifiable clients on live bookings that have no active plan yet (the "Use template" picker)."""
    items = []
    for subject_key, access in accessible_subjects(db, caregiver).items():
        if _active_plan(db, access.require_subject()) is not None:
            continue
        booking = access.booking
        items.append({"booking_id": booking.id, "client": _client(access.require_subject()),
                      "booking_status": booking.status.value, "scheduled_date": booking.scheduled_date})
    items.sort(key=lambda i: (i["booking_status"] != "in_progress", i["scheduled_date"]))
    return {"items": items}


# --- templates ------------------------------------------------------------------------------------------------------

def list_templates(db: Session) -> Dict[str, Any]:
    rows = db.query(CarePlanTemplate).filter(CarePlanTemplate.is_active.is_(True)).order_by(CarePlanTemplate.name).all()
    return {"items": [{k: v for k, v in template_detail(t).items() if k not in ("goals", "tasks", "special_instructions")}
                      for t in rows]}


def get_template(db: Session, template_id: UUID) -> CarePlanTemplate:
    row = db.get(CarePlanTemplate, template_id)
    if row is None or not row.is_active:
        raise TemplateNotFound()
    return row


def template_detail(template: CarePlanTemplate) -> Dict[str, Any]:
    body = template.body or {}
    return {
        "id": template.id, "key": template.key, "name": template.name, "summary": template.summary,
        "duration_days": template.duration_days, "review_every_days": template.review_every_days,
        "default_risk_level": template.default_risk_level, "clinical_review_status": template.clinical_review_status,
        "version": template.version, "goals": body.get("goals", []), "tasks": body.get("tasks", []),
        "special_instructions": body.get("special_instructions", []),
    }


# --- writes ---------------------------------------------------------------------------------------------------------

def record_event(db: Session, plan_id: UUID, actor_type: str, actor_id: Optional[UUID], action: str,
                 detail: Optional[Dict[str, Any]] = None) -> None:
    """Append to the plan's history. ``detail`` is field names, ids and counts only: never health text."""
    db.add(CarePlanEvent(plan_id=plan_id, actor_type=actor_type, actor_id=actor_id, action=action, detail=detail or {}))


def _parse_time(value: Optional[str]) -> Optional[time]:
    return time.fromisoformat(value) if value else None


def _lock_plan(db: Session, plan_id: UUID) -> CarePlan:
    plan = (db.query(CarePlan).filter(CarePlan.id == plan_id).populate_existing().with_for_update().one())
    return plan


def _lock_editable(db: Session, plan_id: UUID, expected_version: Optional[int]) -> CarePlan:
    plan = _lock_plan(db, plan_id)
    if plan.status != "active":
        raise PlanNotEditable()
    if expected_version is not None and plan.version != expected_version:
        raise VersionConflict()
    return plan


def _touch(plan: CarePlan) -> None:
    plan.version += 1
    plan.updated_at = datetime.utcnow()


def _template_content(template: CarePlanTemplate, payload: CarePlanCreate) -> Tuple[List[dict], List[dict], List[str]]:
    body = template.body or {}
    goals = [g.model_dump() for g in payload.goals] if payload.goals is not None else body.get("goals", [])
    tasks = [t.model_dump() for t in payload.tasks] if payload.tasks is not None else body.get("tasks", [])
    notes = payload.special_instructions if payload.special_instructions is not None else body.get("special_instructions", [])
    return goals, tasks, list(notes)


def create_plan(db: Session, caregiver: Caregiver, access: PlanAccess, payload: CarePlanCreate) -> CarePlan:
    subject = access.subject
    if subject is None:
        raise ClientNotIdentified()
    template = get_template(db, payload.template_id) if payload.template_id else None
    existing = _active_plan(db, subject)
    if existing is not None:
        raise CarePlanExists(existing.id)

    if template is not None:
        goals, tasks, instructions = _template_content(template, payload)
    else:
        goals = [g.model_dump() for g in payload.goals or []]
        tasks = [t.model_dump() for t in payload.tasks or []]
        instructions = list(payload.special_instructions or [])
    risk = payload.risk_level or (template.default_risk_level if template else None)
    if risk is None:
        raise InvalidPlan("risk_level is required")
    if len(goals) > MAX_GOALS or len(tasks) > MAX_TASKS:
        raise InvalidPlan("too many goals or tasks")
    duration = payload.duration_days or (template.duration_days if template else DEFAULT_DURATION_DAYS)
    review = payload.review_every_days if payload.review_every_days is not None else (
        template.review_every_days if template else None)

    plan = CarePlan(
        patient_id=subject.id if subject.kind == "patient" else None,
        care_recipient_id=subject.id if subject.kind == "recipient" else None,
        template_id=template.id if template else None, template_version=template.version if template else None,
        risk_level=risk, diagnosis_encrypted=seal(payload.diagnosis),
        allergies_encrypted=seal_list(payload.allergies), special_instructions_encrypted=seal_list(instructions),
        start_date=payload.start_date or local_today(), duration_days=duration, review_every_days=review,
        created_by_caregiver_id=caregiver.id, created_from_booking_id=access.booking.id,
    )
    try:
        db.add(plan)
        db.flush()  # the partial unique index decides a race between two creates here
        for position, goal in enumerate(goals):
            db.add(CarePlanGoal(plan_id=plan.id, title_encrypted=seal(goal["title"]), target_encrypted=seal(goal.get("target")),
                                frequency=goal["frequency"], sort_order=position))
        for position, task in enumerate(tasks):
            db.add(CarePlanTask(plan_id=plan.id, title_encrypted=seal(task["title"]), time_slot=task["time_slot"],
                                time_of_day=_parse_time(task.get("time_of_day")),
                                days_of_week=sorted(task["days_of_week"]), sort_order=position))
        record_event(db, plan.id, "caregiver", caregiver.id, "plan.create",
                     {"goals": len(goals), "tasks": len(tasks), "booking_id": str(access.booking.id)})
        if template is not None:
            record_event(db, plan.id, "caregiver", caregiver.id, "template.use",
                         {"template_id": str(template.id), "template_version": template.version})
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = _active_plan(db, subject)
        if winner is None:
            raise
        raise CarePlanExists(winner.id) from None
    except Exception:
        db.rollback()
        raise
    db.refresh(plan)
    return plan


def update_plan(db: Session, caregiver: Caregiver, plan_id: UUID, payload: CarePlanPatch) -> CarePlan:
    sent = payload.model_fields_set - {"expected_version"}
    try:
        plan = _lock_editable(db, plan_id, payload.expected_version)
        changed: List[str] = []
        if "diagnosis" in sent:
            plan.diagnosis_encrypted = seal(payload.diagnosis)
            changed.append("diagnosis")
        for name in ("risk_level", "start_date", "duration_days", "review_every_days"):
            if name in sent:
                setattr(plan, name, getattr(payload, name))
                changed.append(name)
        if "allergies" in sent:
            plan.allergies_encrypted = seal_list(payload.allergies)
            changed.append("allergies")
        if "special_instructions" in sent:
            plan.special_instructions_encrypted = seal_list(payload.special_instructions)
            changed.append("special_instructions")
        if changed:
            record_event(db, plan.id, "caregiver", caregiver.id, "plan.update", {"fields": changed})
        if payload.status is not None:
            now = datetime.utcnow()
            plan.status = payload.status
            if payload.status == "completed":
                plan.completed_at = now
            else:
                plan.archived_at = now
            record_event(db, plan.id, "caregiver", caregiver.id, "plan.status", {"from": "active", "to": payload.status})
        if changed or payload.status is not None:
            _touch(plan)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(plan)
    return plan


def _goal_for(db: Session, goal_id: UUID) -> Optional[CarePlanGoal]:
    return db.get(CarePlanGoal, goal_id)


def add_goal(db: Session, caregiver: Caregiver, plan_id: UUID, payload: GoalCreate) -> Tuple[CarePlanGoal, int]:
    try:
        plan = _lock_editable(db, plan_id, payload.expected_version)
        live = db.query(CarePlanGoal.id).filter(CarePlanGoal.plan_id == plan.id, CarePlanGoal.status != "discontinued").count()
        if live >= MAX_GOALS:
            raise InvalidPlan(f"a plan can have at most {MAX_GOALS} goals")
        last = db.query(CarePlanGoal.sort_order).filter(CarePlanGoal.plan_id == plan.id).order_by(
            CarePlanGoal.sort_order.desc()).first()
        goal = CarePlanGoal(plan_id=plan.id, title_encrypted=seal(payload.title), target_encrypted=seal(payload.target),
                            frequency=payload.frequency, sort_order=(last[0] + 1) if last else 0)
        db.add(goal)
        db.flush()
        record_event(db, plan.id, "caregiver", caregiver.id, "goal.add", {"goal_id": str(goal.id)})
        _touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(goal)
    return goal, version


def update_goal(db: Session, caregiver: Caregiver, goal: CarePlanGoal, payload: GoalPatch) -> Tuple[CarePlanGoal, int]:
    sent = payload.model_fields_set - {"expected_version"}
    try:
        plan = _lock_editable(db, goal.plan_id, payload.expected_version)
        goal = db.query(CarePlanGoal).filter(CarePlanGoal.id == goal.id).populate_existing().with_for_update().one()
        if goal.status == "discontinued" and payload.status in ("active", "achieved"):
            live = db.query(CarePlanGoal.id).filter(CarePlanGoal.plan_id == plan.id,
                                                    CarePlanGoal.status != "discontinued").count()
            if live >= MAX_GOALS:
                raise InvalidPlan(f"a plan can have at most {MAX_GOALS} goals")
        changed: List[str] = []
        if "title" in sent:
            goal.title_encrypted = seal(payload.title)
            changed.append("title")
        if "target" in sent:
            goal.target_encrypted = seal(payload.target)
            changed.append("target")
        for name in ("frequency", "status"):
            if name in sent:
                setattr(goal, name, getattr(payload, name))
                changed.append(name)
        if changed:
            goal.updated_at = datetime.utcnow()
            record_event(db, plan.id, "caregiver", caregiver.id, "goal.update", {"goal_id": str(goal.id), "fields": changed})
            _touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(goal)
    return goal, version


def remove_goal(db: Session, caregiver: Caregiver, goal: CarePlanGoal, expected_version: int) -> Tuple[CarePlanGoal, int]:
    """Removing a goal marks it discontinued; the row stays so the plan keeps its history."""
    try:
        plan = _lock_editable(db, goal.plan_id, expected_version)
        goal = db.query(CarePlanGoal).filter(CarePlanGoal.id == goal.id).populate_existing().with_for_update().one()
        if goal.status != "discontinued":
            goal.status = "discontinued"
            goal.updated_at = datetime.utcnow()
            record_event(db, plan.id, "caregiver", caregiver.id, "goal.remove", {"goal_id": str(goal.id)})
            _touch(plan)
        version = plan.version
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(goal)
    return goal, version


def goal_out(goal: CarePlanGoal) -> Dict[str, Any]:
    return _goal_out(goal)


# --- erasure --------------------------------------------------------------------------------------------------------

def wipe_plans_for_subject(db: Session, *, patient_id: Optional[UUID] = None, recipient_id: Optional[UUID] = None,
                           now: Optional[datetime] = None) -> int:
    """Account erasure: archive the subject's plans and null every ciphertext (header, goals, tasks, medications, notes). Ids,
    dates, enums, the ticks and the event trail stay. Returns the number of plans wiped. Does not commit."""
    now = now or datetime.utcnow()
    column = CarePlan.patient_id if patient_id is not None else CarePlan.care_recipient_id
    plans = db.query(CarePlan).filter(column == (patient_id or recipient_id)).all()
    for plan in plans:
        plan.status, plan.archived_at = "archived", plan.archived_at or now
        plan.diagnosis_encrypted = plan.allergies_encrypted = plan.special_instructions_encrypted = None
        db.query(CarePlanGoal).filter(CarePlanGoal.plan_id == plan.id).update(
            {"title_encrypted": None, "target_encrypted": None}, synchronize_session=False)
        db.query(CarePlanTask).filter(CarePlanTask.plan_id == plan.id).update(
            {"title_encrypted": None}, synchronize_session=False)
        db.query(CarePlanMedication).filter(CarePlanMedication.plan_id == plan.id).update(
            {"detail_encrypted": None}, synchronize_session=False)
        db.query(CarePlanObservation).filter(CarePlanObservation.plan_id == plan.id).update(
            {"body_encrypted": None}, synchronize_session=False)
        record_event(db, plan.id, "system", None, "plan.erased", {})
    return len(plans)
