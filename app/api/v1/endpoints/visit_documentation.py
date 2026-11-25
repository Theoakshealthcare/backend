"""A caregiver's own documentation for a visit (caregiver routes under /caregiver, admin read under /admin).

Private to the author and the admin console; no patient or family route returns it. Health data: every response is
``Cache-Control: no-store`` and a 422 never echoes the rejected value. See ``app/services/visit_documentation.py`` for the rules.
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_caregiver, get_db, require_permission
from app.api.v1.endpoints.caregiver_care_plans import ScrubbedValidationRoute, _no_store
from app.db.models.admin import AdminUser
from app.db.models.caregiver import Caregiver
from app.db.models.user import User
from app.schemas.visit_documentation import AdminDocumentList, DocumentIn, DocumentList, DocumentOut
from app.services import visit_documentation as svc
from app.services.audit import AuditService

router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)
admin_router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)

READ = require_permission("careplans.read")

ERRORS = {
    404: {"description": "`not_found`: not your visit, or the 24-hour reading window is over."},
    409: {"description": "`visit_not_active`: the visit is not in progress and the 2-hour window after it has closed."},
}


def _caregiver(db: Session, user: User) -> Caregiver:
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == user.id).first()
    if caregiver is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=svc.NOT_FOUND)
    return caregiver


@router.get("/bookings/{booking_id}/documentation", response_model=DocumentList, responses=ERRORS)
def list_documentation(booking_id: UUID, current_user: User = Depends(get_current_caregiver),
                       db: Session = Depends(get_db)) -> Any:
    """My own documentation for this visit, newest first. Only the caller's entries, never anyone else's (not even another
    caregiver's on the same client)."""
    return svc.list_own(db, _caregiver(db, current_user), booking_id)


@router.post("/bookings/{booking_id}/documentation", response_model=DocumentOut, status_code=status.HTTP_201_CREATED,
             responses=ERRORS)
def add_documentation(booking_id: UUID, body: DocumentIn, current_user: User = Depends(get_current_caregiver),
                      db: Session = Depends(get_db)) -> Any:
    """Add an entry to my own documentation for this visit. Any caregiver on the visit may, not only a nurse. While the visit
    is in progress and for 2 hours after it was completed. Entries cannot be edited or deleted: a correction is a new entry.
    **Private**: not shown to the patient or the family."""
    return svc.add_document(db, _caregiver(db, current_user), booking_id, body.body)


@admin_router.get("/visit-documentation", response_model=AdminDocumentList)
def admin_list_documentation(
    request: Request,
    booking_id: Optional[UUID] = Query(None), plan_id: Optional[UUID] = Query(None), caregiver_id: Optional[UUID] = Query(None),
    limit: int = Query(svc.DEFAULT_LIMIT, ge=1, le=svc.MAX_LIMIT), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), admin: AdminUser = Depends(READ),
) -> Any:
    """Caregivers' own documentation, newest first, filterable by visit, plan or caregiver. Needs `careplans.read`.
    **Reading it is audited** (`visit_documentation.view`; the audit row holds the filters and the count, never the text)."""
    page = svc.list_for_admin(db, booking_id, plan_id, caregiver_id, limit, offset)
    AuditService(db).record(
        admin_user_id=admin.id, action="visit_documentation.view", entity_type="visit_documentation", entity_id=None,
        after={"booking_id": str(booking_id) if booking_id else None, "plan_id": str(plan_id) if plan_id else None,
               "caregiver_id": str(caregiver_id) if caregiver_id else None, "returned": len(page["items"])},
        request=request)
    db.commit()
    return page
