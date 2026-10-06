from typing import Any, Optional
from uuid import UUID

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.core.client_ip import client_ip
from app.db.models.audit import AdminAuditLog

SENSITIVE_KEYS = ("password", "token", "secret", "pin", "authorization", "api_key")
REDACTED = "[redacted]"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if any(s in str(k).lower() for s in SENSITIVE_KEYS) else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _clean(value: Optional[dict]) -> Optional[dict]:
    if value is None:
        return None
    return _redact(jsonable_encoder(value))


class AuditService:
    """Writes admin_audit_log rows.

    record() only adds the row to the caller's session (flush, no commit), so the audit row and
    the change it describes commit or roll back together.
    """

    def __init__(self, db: Session):
        self.db = db

    def record(
        self,
        *,
        admin_user_id: UUID,
        action: str,
        entity_type: str,
        entity_id: Optional[Any] = None,
        before: Optional[dict] = None,
        after: Optional[dict] = None,
        reason: Optional[str] = None,
        request: Optional[Request] = None,
    ) -> AdminAuditLog:
        ip = user_agent = None
        if request is not None:
            ip = client_ip(request) if request.client else None
            user_agent = (request.headers.get("user-agent") or "")[:500] or None
        row = AdminAuditLog(
            admin_user_id=admin_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            before=_clean(before),
            after=_clean(after),
            reason=reason,
            ip=ip,
            user_agent=user_agent,
        )
        self.db.add(row)
        self.db.flush()
        return row
