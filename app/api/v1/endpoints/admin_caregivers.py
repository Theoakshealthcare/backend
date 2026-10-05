"""Admin caregiver approval API (AD-FR-7..14). Every route needs an admin and writes an audit row."""

import logging
from typing import Any, Callable, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin, get_db
from app.db.models.caregiver import Caregiver
from app.db.models.user import User
from app.schemas.admin_caregiver import (
    BackgroundCheckDecision,
    DocumentDecision,
    IdentityDecision,
    ReasonIn,
    RequestChangesIn,
    SkillsIn,
)
from app.schemas.caregiver import CaregiverProfile
from app.services.caregiver_approval import (
    CaregiverApprovalService,
    CaregiverNotFound,
    DocumentNotFound,
    InvalidInput,
    StateConflict,
)
from app.services.email import send_caregiver_decision_email

logger = logging.getLogger(__name__)
router = APIRouter()

ListStatus = Literal["incomplete", "under_review", "changes_requested", "approved", "rejected", "suspended"]


def get_service(db: Session = Depends(get_db)) -> CaregiverApprovalService:
    return CaregiverApprovalService(db)


def _run(fn: Callable[[], Any]) -> Any:
    """Call a service method and translate its errors into the API's {"detail": ...} responses."""
    try:
        return fn()
    except CaregiverNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Caregiver not found")
    except DocumentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    except InvalidInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except StateConflict as exc:
        detail: Any = {"message": exc.message, "unmet": exc.unmet} if exc.unmet else exc.message
        raise HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _send_email(**kwargs: Any) -> None:
    # Runs after the response. A mail failure must never undo or hide a committed decision.
    try:
        send_caregiver_decision_email(**kwargs)
    except Exception:
        logger.exception("Failed to send caregiver decision email (%s)", kwargs.get("decision"))


def _notify(background: BackgroundTasks, cg: Caregiver, decision: str, **extra: Any) -> None:
    background.add_task(
        _send_email, email_to=cg.email, full_name=cg.full_name, decision=decision, **extra
    )


def _summary(cg: Caregiver) -> Dict[str, Any]:
    return {
        "id": cg.id,
        "profile_status": cg.profile_status,
        "identity_verification_status": cg.identity_verification_status,
        "background_check_status": cg.background_check_status,
    }


@router.get("")
@router.get("/", include_in_schema=False)
def list_applications(
    profile_status: ListStatus = "under_review",
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    return service.list_applications(profile_status, limit, offset)


@router.post("/documents/{doc_id}/decision")
def decide_document(
    doc_id: UUID, body: DocumentDecision, request: Request,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    doc = _run(lambda: service.decide_document(
        doc_id, admin.id, body.decision, body.reason, body.expires_on, request))
    return {
        "id": doc.id, "caregiver_id": doc.caregiver_id, "verification_status": doc.verification_status,
        "rejection_reason": doc.rejection_reason, "expires_on": doc.expires_on, "verified_at": doc.verified_at,
    }


@router.get("/{caregiver_id}")
def get_application(
    caregiver_id: UUID, request: Request,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    detail = _run(lambda: service.get_detail(caregiver_id, admin.id, request))
    detail["profile"] = CaregiverProfile.model_validate(detail["profile"])
    return detail


@router.post("/{caregiver_id}/identity-decision")
def identity_decision(
    caregiver_id: UUID, body: IdentityDecision, request: Request,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    return _summary(_run(lambda: service.decide_identity(
        caregiver_id, admin.id, body.decision, body.notes, request)))


@router.post("/{caregiver_id}/background-check-decision")
def background_check_decision(
    caregiver_id: UUID, body: BackgroundCheckDecision, request: Request,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    return _summary(_run(lambda: service.decide_background_check(
        caregiver_id, admin.id, body.decision, body.notes, body.provider_reference,
        body.document_id, request)))


@router.put("/{caregiver_id}/skills")
def set_skills(
    caregiver_id: UUID, body: SkillsIn, request: Request,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    skills = _run(lambda: service.set_skills(
        caregiver_id, admin.id, [s.model_dump() for s in body.skills], request))
    return {"skills": [{"service_type": s.service_type, "sub_specialty": s.sub_specialty} for s in skills]}


@router.post("/{caregiver_id}/approve")
def approve(
    caregiver_id: UUID, request: Request, background: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    cg = _run(lambda: service.approve(caregiver_id, admin.id, request))
    _notify(background, cg, "approved")
    return _summary(cg)


@router.post("/{caregiver_id}/reject")
def reject(
    caregiver_id: UUID, body: ReasonIn, request: Request, background: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    cg = _run(lambda: service.reject(caregiver_id, admin.id, body.reason, request))
    _notify(background, cg, "rejected", reason=body.reason)
    return _summary(cg)


@router.post("/{caregiver_id}/request-changes")
def request_changes(
    caregiver_id: UUID, body: RequestChangesIn, request: Request, background: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    cg = _run(lambda: service.request_changes(caregiver_id, admin.id, body.items, body.note, request))
    _notify(background, cg, "changes_requested", items=body.items, note=body.note)
    return {**_summary(cg), "changes_requested": cg.changes_requested}


@router.post("/{caregiver_id}/suspend")
def suspend(
    caregiver_id: UUID, body: ReasonIn, request: Request, background: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    cg, affected = _run(lambda: service.suspend(caregiver_id, admin.id, body.reason, request))
    if affected:
        logger.warning("Caregiver %s suspended with %d upcoming accepted booking(s) needing reassignment",
                       cg.id, len(affected))
    _notify(background, cg, "suspended", reason=body.reason)
    return {**_summary(cg), "affected_bookings": affected}


@router.post("/{caregiver_id}/reinstate")
def reinstate(
    caregiver_id: UUID, request: Request,
    admin: User = Depends(get_current_admin),
    service: CaregiverApprovalService = Depends(get_service),
) -> Any:
    return _summary(_run(lambda: service.reinstate(caregiver_id, admin.id, request)))
