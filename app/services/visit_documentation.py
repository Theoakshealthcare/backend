"""A caregiver's own documentation for a visit (clinician pack, section 1: "a space for the caregiver to make own documentation").

Private by design (owner decision, 2026-11-25): the entry is read only by its author and by an admin with ``careplans.read``;
no patient or family route returns it, and it never feeds the plan's ``family_visible`` items. Any caregiver on the visit may
write one, not only a nurse. It follows the typed- and voice-note windows (``app/services/visit_windows.py``): write while the
visit is in progress and for ``VOICE_NOTE_POST_VISIT_GRACE_MINUTES`` after it was completed (409 ``visit_not_active``
afterwards); read your own entries while the visit is live and for ``VOICE_NOTE_EDIT_WINDOW_HOURS`` after it ended (404
afterwards). Append-only: there is no edit and no delete, a correction is a new entry. The body is Fernet ciphertext opened only
when a response is built; audit rows and logs carry ids and filters, never the text.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.models.admin import AdminUser
from app.db.models.booking import Booking, BookingStatus
from app.db.models.care_plan import CarePlan, VisitDocumentation
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.services import visit_windows
from app.services.care_plan_access import is_self_booking
from app.services.care_plans import seal, unseal

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
NOT_FOUND = {"code": "not_found", "message": "Visit not found."}


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND, headers={"Cache-Control": "no-store"})


def _not_active() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT,
                         detail={"code": "visit_not_active", "message": visit_windows.NOTES_CLOSED_MESSAGE},
                         headers={"Cache-Control": "no-store"})


def _owned_booking(db: Session, caregiver: Caregiver, booking_id: UUID) -> Optional[Booking]:
    return (db.query(Booking).join(Patient, Patient.id == Booking.patient_id)
            .filter(Booking.id == booking_id, Booking.caregiver_id == caregiver.id, Patient.is_active.is_(True)).first())


def _client_plan_id(db: Session, booking: Booking) -> Optional[UUID]:
    """The client's active care plan, when the client can be identified and has one (null otherwise)."""
    if is_self_booking(booking):
        column, key = CarePlan.patient_id, booking.patient_id
    elif booking.care_recipient_id is not None:
        column, key = CarePlan.care_recipient_id, booking.care_recipient_id
    else:
        return None
    row = db.query(CarePlan.id).filter(column == key, CarePlan.status == "active").first()
    return row[0] if row else None


def _out(doc: VisitDocumentation) -> Dict[str, Any]:
    return {"id": doc.id, "booking_id": doc.booking_id, "plan_id": doc.plan_id, "created_at": doc.created_at,
            "body": unseal(doc.body_encrypted, doc.plan_id or doc.booking_id, "visit_documentation.body_encrypted") or ""}


def add_document(db: Session, caregiver: Caregiver, booking_id: UUID, body: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.utcnow()
    booking = _owned_booking(db, caregiver, booking_id)
    if booking is None:
        raise _not_found()
    if not visit_windows.can_start_note(caregiver, booking, now):
        if booking.status != BookingStatus.IN_PROGRESS and caregiver.profile_status != "approved":
            raise _not_found()  # a suspended caregiver has no access outside a visit in progress
        raise _not_active()
    try:
        doc = VisitDocumentation(caregiver_id=caregiver.id, booking_id=booking.id, plan_id=_client_plan_id(db, booking),
                                body_encrypted=seal(body), created_at=now)
        db.add(doc)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(doc)
    return _out(doc)


def list_own(db: Session, caregiver: Caregiver, booking_id: UUID, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The caller's own entries for the visit, newest first, with whether they can add one now and until when."""
    now = now or datetime.utcnow()
    booking = _owned_booking(db, caregiver, booking_id)
    if booking is None or not visit_windows.can_read_notes(caregiver, booking, now):
        raise _not_found()
    rows = (db.query(VisitDocumentation)
            .filter(VisitDocumentation.booking_id == booking.id, VisitDocumentation.caregiver_id == caregiver.id)
            .order_by(VisitDocumentation.created_at.desc(), VisitDocumentation.id.desc()).all())
    window = visit_windows.notes_window(caregiver, booking, now)
    return {"items": [_out(r) for r in rows], "can_add": window.open, "closes_at": window.closes_at,
            "readable_until": visit_windows.notes_readable_until(booking)}


def list_for_admin(db: Session, booking_id: Optional[UUID], plan_id: Optional[UUID], caregiver_id: Optional[UUID],
                   limit: int, offset: int) -> Dict[str, Any]:
    """Every entry matching the filters, newest first, with the author's name. The route audits the read."""
    query = db.query(VisitDocumentation)
    if booking_id is not None:
        query = query.filter(VisitDocumentation.booking_id == booking_id)
    if plan_id is not None:
        query = query.filter(VisitDocumentation.plan_id == plan_id)
    if caregiver_id is not None:
        query = query.filter(VisitDocumentation.caregiver_id == caregiver_id)
    total = query.count()
    rows: List[VisitDocumentation] = (query.order_by(VisitDocumentation.created_at.desc(), VisitDocumentation.id.desc())
                                     .offset(offset).limit(limit).all())
    names = dict(db.query(Caregiver.id, Caregiver.full_name)
                 .filter(Caregiver.id.in_({r.caregiver_id for r in rows})).all()) if rows else {}
    return {"items": [{**_out(r), "caregiver": {"id": r.caregiver_id, "name": names.get(r.caregiver_id, "Caregiver")}}
                      for r in rows], "total": total, "limit": limit, "offset": offset}


def blank_for_patient(db: Session, patient_id: UUID) -> int:
    """Account erasure: blank the text of every entry written for this patient's visits. Rows (ids, times) stay. No commit."""
    ids = [i for (i,) in db.query(Booking.id).filter(Booking.patient_id == patient_id)]
    if not ids:
        return 0
    return (db.query(VisitDocumentation).filter(VisitDocumentation.booking_id.in_(ids))
            .update({"body_encrypted": None}, synchronize_session=False))
