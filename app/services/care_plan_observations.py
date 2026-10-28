"""Care plan observations: visit notes (slice B2).

Append-only. There is no update and no delete here, on purpose (a correction is a new note), and no database trigger,
because account erasure must be able to blank the body. The body is Fernet ciphertext opened only when a response is built.
Any caregiver assigned to the client may add a note during their in-progress visit (assumption A4), not only a nurse. Notes
never bump the plan's version and never go to a family member in v1 (``family_visible`` defaults to false and nothing sets it).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.admin import AdminUser
from app.db.models.care_plan import CarePlan, CarePlanObservation
from app.db.models.caregiver import Caregiver
from app.schemas.care_plan import ObservationIn
from app.services.care_plan_access import PlanAccess
from app.services.care_plan_tasks import lock_active_plan, require_live_visit
from app.services.care_plans import record_event, seal, unseal

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _out(obs: CarePlanObservation, plan_id: UUID, names: Dict[str, str]) -> Dict[str, Any]:
    if obs.author_caregiver_id is not None:
        author = {"name": names.get(f"c:{obs.author_caregiver_id}", "Caregiver"), "kind": "caregiver"}
    else:
        author = {"name": names.get(f"a:{obs.author_admin_id}", "Clinical Reviewer"), "kind": "admin"}
    return {"id": obs.id, "category": obs.category, "author": author, "created_at": obs.created_at,
            "body": unseal(obs.body_encrypted, plan_id, "care_plan_observations.body_encrypted") or ""}


def _names(db: Session, rows: List[CarePlanObservation]) -> Dict[str, str]:
    caregivers = {r.author_caregiver_id for r in rows if r.author_caregiver_id}
    admins = {r.author_admin_id for r in rows if r.author_admin_id}
    names = {f"c:{i}": n for i, n in db.query(Caregiver.id, Caregiver.full_name).filter(Caregiver.id.in_(caregivers))} \
        if caregivers else {}
    if admins:
        names.update({f"a:{i}": n for i, n in db.query(AdminUser.id, AdminUser.full_name).filter(AdminUser.id.in_(admins))})
    return names


def add_observation(db: Session, caregiver: Caregiver, access: PlanAccess, plan: CarePlan,
                    payload: ObservationIn) -> Dict[str, Any]:
    try:
        lock_active_plan(db, plan.id)
        booking = require_live_visit(db, caregiver, access.booking.id)
        obs = CarePlanObservation(plan_id=plan.id, booking_id=booking.id, author_caregiver_id=caregiver.id,
                                  category=payload.category, body_encrypted=seal(payload.body))
        db.add(obs)
        db.flush()
        record_event(db, plan.id, "caregiver", caregiver.id, "observation.add",
                     {"observation_id": str(obs.id), "category": obs.category})
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(obs)
    return _out(obs, plan.id, {f"c:{caregiver.id}": caregiver.full_name})


def list_observations(db: Session, plan: CarePlan, before: Optional[datetime], limit: int,
                      category: Optional[str]) -> Dict[str, Any]:
    """Newest first, keyset paged on ``created_at``: pass the previous page's ``next_before`` as ``before``."""
    query = db.query(CarePlanObservation).filter(CarePlanObservation.plan_id == plan.id)
    if category is not None:
        query = query.filter(CarePlanObservation.category == category)
    if before is not None:
        query = query.filter(CarePlanObservation.created_at < before)
    rows = query.order_by(CarePlanObservation.created_at.desc(), CarePlanObservation.id.desc()).limit(limit + 1).all()
    more, rows = len(rows) > limit, rows[:limit]
    names = _names(db, rows)
    return {"items": [_out(r, plan.id, names) for r in rows], "next_before": rows[-1].created_at if more else None}
