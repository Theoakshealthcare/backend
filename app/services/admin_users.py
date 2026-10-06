"""Admin management for Super Admins: invite, list, change roles or status, reset MFA (AD-FR-83),
and the audit viewer query (AD-FR-85)."""

import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.admin_permissions import SUPER_ADMIN
from app.core.admin_security import hash_token, new_opaque_token
from app.core.config import settings
from app.db.models.admin import AdminRole, AdminUser
from app.db.models.audit import AdminAuditLog
from app.services.admin_auth import normalize_email, revoke_all_sessions
from app.services.audit import AuditService

# Serialises every change that could remove a Super Admin, so two concurrent changes cannot each
# see "someone else is still a Super Admin" and leave none.
_SUPER_ADMIN_LOCK = "oakly.admin_users.super_admin_guard"


class AdminUserError(Exception):
    pass


class AdminNotFound(AdminUserError):
    pass


class AdminConflict(AdminUserError):
    pass


class InvalidInput(AdminUserError):
    pass


def admin_summary(admin: AdminUser) -> Dict[str, Any]:
    return {
        "id": admin.id, "email": admin.email, "full_name": admin.full_name, "status": admin.status,
        "mfa_enabled": admin.mfa_enabled, "roles": admin.role_keys,
        "last_login_at": admin.last_login_at, "locked_until": admin.locked_until,
        "created_at": admin.created_at,
    }


def me_payload(admin: AdminUser) -> Dict[str, Any]:
    return {"id": admin.id, "email": admin.email, "full_name": admin.full_name,
            "roles": admin.role_keys, "permissions": admin.permission_list}


class AdminUserService:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    def _roles(self, keys: Sequence[str]) -> List[AdminRole]:
        wanted = sorted(set(keys))
        found = list(self.db.scalars(select(AdminRole).where(AdminRole.key.in_(wanted))))
        missing = sorted(set(wanted) - {r.key for r in found})
        if missing:
            raise InvalidInput(f"Unknown role: {', '.join(missing)}")
        return found

    def _lock_super_admin_guard(self) -> None:
        self.db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _SUPER_ADMIN_LOCK})

    def _other_active_super_admins(self, excluding: uuid.UUID) -> int:
        return self.db.scalar(
            select(func.count(AdminUser.id))
            .join(AdminUser.roles)
            .where(AdminRole.key == SUPER_ADMIN, AdminUser.status == "active", AdminUser.id != excluding)
        ) or 0

    @staticmethod
    def _is_active_super(admin: AdminUser) -> bool:
        return admin.status == "active" and SUPER_ADMIN in admin.role_keys

    def _get(self, admin_id: uuid.UUID) -> AdminUser:
        admin = self.db.get(AdminUser, admin_id, with_for_update=True, populate_existing=True)
        if admin is None:
            raise AdminNotFound()
        return admin

    # --- commands ------------------------------------------------------------------------------

    def invite(self, actor: AdminUser, email: str, full_name: str, role_keys: Sequence[str],
               request: Request) -> Tuple[AdminUser, str]:
        email = normalize_email(email)
        roles = self._roles(role_keys)
        if self.db.scalar(select(AdminUser.id).where(AdminUser.email == email)) is not None:
            raise AdminConflict("An admin with this email already exists")
        raw = new_opaque_token()
        admin = AdminUser(
            email=email, full_name=full_name.strip(), status="invited", roles=roles,
            invite_token_hash=hash_token(raw),
            invite_expires_at=datetime.utcnow() + timedelta(hours=settings.ADMIN_INVITE_HOURS),
            created_by=actor.id,
        )
        self.db.add(admin)
        self.db.flush()
        self.audit.record(
            admin_user_id=actor.id, action="admin.invite", entity_type="admin_user", entity_id=admin.id,
            after={"email": email, "full_name": admin.full_name, "roles": admin.role_keys}, request=request,
        )
        self.db.commit()
        self.db.refresh(admin)
        return admin, raw

    def update(self, actor: AdminUser, admin_id: uuid.UUID, role_keys: Optional[Sequence[str]],
               new_status: Optional[str], request: Request) -> AdminUser:
        self._lock_super_admin_guard()
        admin = self._get(admin_id)
        before = {"roles": admin.role_keys, "status": admin.status}
        was_super = self._is_active_super(admin)

        roles = self._roles(role_keys) if role_keys is not None else None
        if new_status is not None and new_status != admin.status:
            if new_status == "disabled" and admin.id == actor.id:
                raise AdminConflict("You cannot disable your own account")
            if new_status == "active" and admin.password_hash is None:
                raise AdminConflict("This admin has not accepted their invite yet")
        target_status = new_status or admin.status
        target_roles = {r.key for r in roles} if roles is not None else set(admin.role_keys)

        if was_super and not (target_status == "active" and SUPER_ADMIN in target_roles):
            if self._other_active_super_admins(admin.id) == 0:
                raise AdminConflict("Cannot remove or disable the last active Super Admin")

        if roles is not None:
            admin.roles = roles
        if target_status != admin.status:
            admin.status = target_status
            if target_status == "disabled":
                revoke_all_sessions(self.db, admin.id)
                admin.invite_token_hash = None
                admin.invite_expires_at = None
        self.db.flush()
        self.audit.record(
            admin_user_id=actor.id, action="admin.update", entity_type="admin_user", entity_id=admin.id,
            before=before, after={"roles": admin.role_keys, "status": admin.status}, request=request,
        )
        self.db.commit()
        self.db.refresh(admin)
        return admin

    def reset_mfa(self, actor: AdminUser, admin_id: uuid.UUID, request: Request) -> AdminUser:
        admin = self._get(admin_id)
        admin.mfa_secret_enc = None
        admin.mfa_enabled = False
        admin.mfa_last_step = None
        # also lifts a lockout: this is the recovery path for an admin who lost their device
        admin.failed_logins = 0
        admin.mfa_lock_count = 0
        admin.locked_until = None
        revoke_all_sessions(self.db, admin.id)
        self.audit.record(admin_user_id=actor.id, action="admin.reset_mfa", entity_type="admin_user",
                          entity_id=admin.id, request=request)
        self.db.commit()
        self.db.refresh(admin)
        return admin

    # --- queries -------------------------------------------------------------------------------

    def list_admins(self) -> List[AdminUser]:
        return list(self.db.scalars(select(AdminUser).order_by(AdminUser.created_at, AdminUser.email)))

    def audit_page(
        self, *, entity_type: Optional[str], entity_id: Optional[str], admin_user_id: Optional[uuid.UUID],
        action: Optional[str], date_from: Optional[date], date_to: Optional[date], limit: int, offset: int,
    ) -> Dict[str, Any]:
        conds = []
        if entity_type:
            conds.append(AdminAuditLog.entity_type == entity_type)
        if entity_id:
            conds.append(AdminAuditLog.entity_id == entity_id)
        if admin_user_id:
            conds.append(AdminAuditLog.admin_user_id == admin_user_id)
        if action:
            conds.append(AdminAuditLog.action == action)
        if date_from:
            conds.append(AdminAuditLog.created_at >= datetime.combine(date_from, datetime.min.time()))
        if date_to:  # inclusive of the whole day
            conds.append(AdminAuditLog.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
        total = self.db.scalar(select(func.count(AdminAuditLog.id)).where(*conds)) or 0
        rows = self.db.execute(
            select(AdminAuditLog, AdminUser.email)
            .join(AdminUser, AdminUser.id == AdminAuditLog.admin_user_id)
            .where(*conds)
            .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id)
            .limit(limit).offset(offset)
        ).all()
        items = [
            {"id": r.id, "admin_user_id": r.admin_user_id, "admin_email": email, "action": r.action,
             "entity_type": r.entity_type, "entity_id": r.entity_id, "before": r.before, "after": r.after,
             "reason": r.reason, "ip": r.ip, "user_agent": r.user_agent, "created_at": r.created_at}
            for r, email in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
