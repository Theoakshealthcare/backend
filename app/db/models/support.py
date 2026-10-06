import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

TICKET_REASONS = ("talk_to_person", "two_misses", "emergency", "data_withdrawal", "booking_placeholder")


class SupportConversation(Base):
    """A human-handover ticket: what the Admin console inbox will list.

    No transcript here. A handover transcript, when it is built, is a separate, shorter-retention record.
    """
    __tablename__ = "support_conversations"
    __table_args__ = (
        CheckConstraint("priority IN ('normal', 'urgent')", name="ck_support_conversations_priority"),
        CheckConstraint("status IN ('open', 'assigned', 'resolved')", name="ck_support_conversations_status"),
        CheckConstraint(
            "reason IN (" + ", ".join(f"'{r}'" for r in TICKET_REASONS) + ")",
            name="ck_support_conversations_reason",
        ),
        Index("ix_support_conversations_queue", "status", "priority", "created_at"),
        # One live handover per contact. A data-withdrawal task is a compliance record that must not be
        # merged into (or hidden by) a handover, so it is exempt.
        Index(
            "uq_support_conversations_one_open_handover", "contact_id", unique=True,
            postgresql_where=text("status <> 'resolved' AND reason <> 'data_withdrawal'"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("whatsapp_contacts.id"), nullable=False, index=True)
    reason = Column(String, nullable=False)
    priority = Column(String, nullable=False, default="normal", server_default="normal")
    status = Column(String, nullable=False, default="open", server_default="open")
    assigned_agent_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    resolved_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
