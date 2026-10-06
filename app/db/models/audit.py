import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, String, Text, event
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class AdminAuditLog(Base):
    """Append-only record of what an admin did. Rows are never updated or deleted.

    The ORM refuses UPDATE/DELETE below, and the Alembic migration adds a Postgres trigger
    that refuses them for any client.
    """
    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("ix_admin_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_admin_audit_log_admin_created", "admin_user_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_user_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=False)
    action = Column(String, nullable=False)  # e.g. "caregiver.approve"
    entity_type = Column(String, nullable=False)
    entity_id = Column(String, nullable=True)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    reason = Column(Text, nullable=True)
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


@event.listens_for(AdminAuditLog, "before_update")
def _refuse_update(mapper, connection, target):
    raise RuntimeError("admin_audit_log is append-only")


@event.listens_for(AdminAuditLog, "before_delete")
def _refuse_delete(mapper, connection, target):
    raise RuntimeError("admin_audit_log is append-only")
