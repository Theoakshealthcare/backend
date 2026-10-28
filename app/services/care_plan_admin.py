"""Care plans, admin side (slice B4): the Clinical Reviewer reads, authors and signs off through the admin API.

Writes are the caregiver services' own (``care_plans``, ``care_plan_tasks``, ``care_plan_medications``,
``care_plan_observations``) called with an ``AdminActor``; this module adds what only an admin does: name the client, list
across clients, and review templates. Every admin write is recorded by ``care_plans.actor_event`` as a plan event and an
``admin_audit_log`` row in the same transaction. Reading a plan or its notes is itself audited (as ``booking.clinical_view``
is), written and committed before the response leaves. The audit rows carry field names and ids, never plan text.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.admin import AdminUser
from app.db.models.care import CareRecipient
from app.db.models.caregiver import Caregiver
from app.db.models.care_plan import CarePlan, CarePlanGoal, CarePlanMedication, CarePlanTask, CarePlanTemplate
from app.db.models.patient import Patient
from app.schemas.admin_care_plan import AdminCarePlanCreate, TemplateReviewIn
from app.services import care_plans as base
from app.services.audit import AuditService
from app.services.account_erasure import ERASED_NAME
from app.services.care_plan_access import Subject
from app.services.care_plans import AdminActor, CarePlanError

LIST_MAX = 200
# What opening a plan shows, for the audit row (names only, like ``booking.clinical_view``).
PLAN_FIELDS = ["diagnosis", "allergies", "special_instructions", "goals", "medications", "tasks"]


class PlanNotFound(CarePlanError):
    code, message = "plan_not_found", "Care plan not found"


class ClientNotFound(CarePlanError):
    code, message = "client_not_found", "No such client."


# --- the client -------------------------------------------------------------------------------------------------------

def _subject(kind: str, row: Any) -> Subject:
    return Subject(kind, row.id, row.full_name, row.date_of_birth, row.gender)


def resolve_client(db: Session, payload: AdminCarePlanCreate) -> Subject:
    """The client a plan is for. A patient must be active and a care recipient must not be an erased one (erasure sets the
    patient inactive and renames the person's own recipient row): an erased person gets no new health text."""
    if payload.patient_id is not None:
        patient = db.get(Patient, payload.patient_id)
        if patient is None or not patient.is_active:
            raise ClientNotFound()
        return _subject("patient", patient)
    recipient = db.get(CareRecipient, payload.care_recipient_id)
    if recipient is None or recipient.full_name == ERASED_NAME:  # erasure renames the person's own recipient row
        raise ClientNotFound()
    return _subject("recipient", recipient)


def subject_of(db: Session, plan: CarePlan) -> Subject:
    """The plan's client, active or not (an erased client's archived plan is still readable, with its text wiped)."""
    if plan.patient_id is not None:
        return _subject("patient", db.get(Patient, plan.patient_id))
    return _subject("recipient", db.get(CareRecipient, plan.care_recipient_id))


def get_plan(db: Session, plan_id: UUID) -> CarePlan:
    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise PlanNotFound()
    return plan


# --- reads ------------------------------------------------------------------------------------------------------------

def build_admin_plan(db: Session, plan: CarePlan) -> Dict[str, Any]:
    subject = subject_of(db, plan)
    return {**base.plan_core(db, plan, subject), "subject": {"kind": subject.kind, "id": subject.id}}


def view_plan(db: Session, actor: AdminActor, plan_id: UUID) -> Dict[str, Any]:
    """Open a plan. The open is audited (``care_plan.view``) and committed before anything is returned."""
    plan = get_plan(db, plan_id)
    body = build_admin_plan(db, plan)
    AuditService(db).record(admin_user_id=actor.admin.id, action="care_plan.view", entity_type="care_plan",
                            entity_id=plan.id, after={"fields": PLAN_FIELDS}, request=actor.request)
    db.commit()
    return body


def audit_unchanged_read(db: Session, actor: AdminActor, plan_id: UUID, via: str) -> None:
    """A response that carries plan text without a write row behind it (a stale-version 409 shows the current plan; a write
    that changed nothing returns it too) is a read, so it is audited as one."""
    AuditService(db).record(admin_user_id=actor.admin.id, action="care_plan.view", entity_type="care_plan",
                            entity_id=plan_id, after={"fields": PLAN_FIELDS, "via": via}, request=actor.request)
    db.commit()


def audit_notes_view(db: Session, actor: AdminActor, plan: CarePlan) -> None:
    AuditService(db).record(admin_user_id=actor.admin.id, action="care_plan.observations_view", entity_type="care_plan",
                            entity_id=plan.id, after={"fields": ["observations"]}, request=actor.request)
    db.commit()


def _counts(db: Session, plan_ids: List[UUID]) -> Dict[str, Dict[UUID, int]]:
    def grouped(column, *conditions):
        if not plan_ids:
            return {}
        return dict(db.query(column, func.count()).filter(column.in_(plan_ids), *conditions).group_by(column).all())

    return {
        "goals": grouped(CarePlanGoal.plan_id, CarePlanGoal.status != "discontinued"),
        "tasks": grouped(CarePlanTask.plan_id, CarePlanTask.is_active.is_(True)),
        "medications": grouped(CarePlanMedication.plan_id, CarePlanMedication.status == "active"),
    }


def _like(needle: str) -> str:
    return "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_plans(db: Session, status: Optional[str], q: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
    """Plans across clients, newest change first. The name filter is on the client's name only (the diagnosis is encrypted)."""
    limit = min(limit, LIST_MAX)
    name = func.coalesce(Patient.full_name, CareRecipient.full_name)
    query = (db.query(CarePlan, Patient, CareRecipient)
             .outerjoin(Patient, Patient.id == CarePlan.patient_id)
             .outerjoin(CareRecipient, CareRecipient.id == CarePlan.care_recipient_id))
    if status:
        query = query.filter(CarePlan.status == status)
    if q and q.strip():
        query = query.filter(name.ilike(_like(q.strip()), escape="\\"))
    total = query.count()
    rows = query.order_by(CarePlan.updated_at.desc(), CarePlan.id).limit(limit).offset(offset).all()
    counts = _counts(db, [plan.id for plan, _, _ in rows])
    caregivers = {p.created_by_caregiver_id for p, _, _ in rows if p.created_by_caregiver_id}
    admins = {p.created_by_admin_id for p, _, _ in rows if p.created_by_admin_id}
    names = {("caregiver", i): n for i, n in db.query(Caregiver.id, Caregiver.full_name).filter(Caregiver.id.in_(caregivers))} \
        if caregivers else {}
    if admins:
        names.update({("admin", i): n for i, n in db.query(AdminUser.id, AdminUser.full_name).filter(AdminUser.id.in_(admins))})
    items = []
    for plan, patient, recipient in rows:
        subject = _subject("patient", patient) if patient is not None else _subject("recipient", recipient)
        items.append({
            "id": plan.id, "version": plan.version, "status": plan.status,
            "subject": {"kind": subject.kind, "id": subject.id}, "client": base._client(subject),
            "risk_level": plan.risk_level, "start_date": plan.start_date, "duration_days": plan.duration_days,
            "author": ({"name": names.get(("caregiver", plan.created_by_caregiver_id), "Caregiver"), "kind": "caregiver"}
                       if plan.created_by_caregiver_id else
                       {"name": names.get(("admin", plan.created_by_admin_id), "Clinical Reviewer"), "kind": "admin"}),
            "goal_count": counts["goals"].get(plan.id, 0),
            "task_count": counts["tasks"].get(plan.id, 0), "medication_count": counts["medications"].get(plan.id, 0),
            "updated_at": plan.updated_at,
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# --- create -----------------------------------------------------------------------------------------------------------

def create_plan(db: Session, actor: AdminActor, payload: AdminCarePlanCreate) -> CarePlan:
    """Create the client's plan, blank or from a template. Same rules as a nurse's (one active plan per client, limits)."""
    return base.create_plan_for_subject(db, actor, resolve_client(db, payload), None, payload)


# --- templates --------------------------------------------------------------------------------------------------------

def _template_out(template: CarePlanTemplate, reviewer: Optional[str]) -> Dict[str, Any]:
    return {**base.template_detail(template), "is_active": template.is_active, "reviewed_by": reviewer,
            "reviewed_at": template.reviewed_at}


def _reviewer_names(db: Session, templates: List[CarePlanTemplate]) -> Dict[UUID, str]:
    ids = {t.reviewed_by_admin_id for t in templates if t.reviewed_by_admin_id}
    return dict(db.query(AdminUser.id, AdminUser.full_name).filter(AdminUser.id.in_(ids)).all()) if ids else {}


def list_templates(db: Session) -> Dict[str, Any]:
    """Every template, active or not, with its review state and full content (a reviewer signs off what they have read)."""
    rows = db.query(CarePlanTemplate).order_by(CarePlanTemplate.name).all()
    names = _reviewer_names(db, rows)
    return {"items": [_template_out(t, names.get(t.reviewed_by_admin_id)) for t in rows]}


def review_template(db: Session, actor: AdminActor, template_id: UUID, payload: TemplateReviewIn) -> Dict[str, Any]:
    """Sign a template off (``approved``) or take the sign-off back (``pending``). Always applied and audited, so a second
    reviewer's sign-off is recorded too. The audit row carries the template key and version, not its content."""
    try:
        template = (db.query(CarePlanTemplate).filter(CarePlanTemplate.id == template_id).populate_existing()
                    .with_for_update().first())
        if template is None:
            raise base.TemplateNotFound()
        before = template.clinical_review_status
        template.clinical_review_status = payload.status
        template.reviewed_by_admin_id = actor.admin.id
        template.reviewed_at = datetime.utcnow()
        template.updated_at = template.reviewed_at
        AuditService(db).record(
            admin_user_id=actor.admin.id, action="care_plan_template.review", entity_type="care_plan_template",
            entity_id=template.id, before={"clinical_review_status": before},
            after={"clinical_review_status": payload.status, "key": template.key, "version": template.version},
            request=actor.request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(template)
    return _template_out(template, actor.admin.full_name)
