"""Admin identity and management: /admin/me, /admin/admins, /admin/audit (AD-FR-83, AD-FR-85)."""

from datetime import date
from typing import Any, Callable, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin_user, get_db, require_permission
from app.db.models.admin import AdminUser
from app.schemas.admin_auth import (
    AdminInviteIn,
    AdminInviteOut,
    AdminListItem,
    AdminOut,
    AdminPatchIn,
    AuditPageOut,
)
from app.services.admin_users import (
    AdminConflict,
    AdminNotFound,
    AdminUserService,
    InvalidInput,
    admin_summary,
    me_payload,
)

router = APIRouter()


def get_service(db: Session = Depends(get_db)) -> AdminUserService:
    return AdminUserService(db)


def _run(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except AdminNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Admin not found")
    except AdminConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/me", response_model=AdminOut)
def me(admin: AdminUser = Depends(get_current_admin_user)) -> Any:
    return me_payload(admin)


@router.get("/admins", response_model=List[AdminListItem])
def list_admins(
    _admin: AdminUser = Depends(require_permission("admins.manage")),
    service: AdminUserService = Depends(get_service),
) -> Any:
    return [admin_summary(a) for a in service.list_admins()]


@router.post("/admins", response_model=AdminInviteOut, status_code=status.HTTP_201_CREATED)
def invite_admin(
    body: AdminInviteIn, request: Request,
    admin: AdminUser = Depends(require_permission("admins.manage")),
    service: AdminUserService = Depends(get_service),
) -> Any:
    created, token = _run(lambda: service.invite(admin, body.email, body.full_name, body.roles, request))
    return {**admin_summary(created), "invite_token": token, "invite_expires_at": created.invite_expires_at}


@router.patch("/admins/{admin_id}", response_model=AdminListItem)
def update_admin(
    admin_id: UUID, body: AdminPatchIn, request: Request,
    admin: AdminUser = Depends(require_permission("admins.manage")),
    service: AdminUserService = Depends(get_service),
) -> Any:
    updated = _run(lambda: service.update(admin, admin_id, body.roles, body.status, request))
    return admin_summary(updated)


@router.post("/admins/{admin_id}/reset-mfa", response_model=AdminListItem)
def reset_admin_mfa(
    admin_id: UUID, request: Request,
    admin: AdminUser = Depends(require_permission("admins.manage")),
    service: AdminUserService = Depends(get_service),
) -> Any:
    return admin_summary(_run(lambda: service.reset_mfa(admin, admin_id, request)))


@router.get("/audit", response_model=AuditPageOut)
def audit_log(
    entity_type: Optional[str] = Query(None, max_length=100),
    entity_id: Optional[str] = Query(None, max_length=100),
    admin_user_id: Optional[UUID] = None,
    action: Optional[str] = Query(None, max_length=100),
    date_from: Optional[date] = Query(None, description="Inclusive, UTC"),
    date_to: Optional[date] = Query(None, description="Inclusive, UTC"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: AdminUser = Depends(require_permission("audit.read")),
    service: AdminUserService = Depends(get_service),
) -> Any:
    return service.audit_page(
        entity_type=entity_type, entity_id=entity_id, admin_user_id=admin_user_id, action=action,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    )
