"""WhatsApp channel tables (PRD section 10).

``whatsapp_messages`` holds metadata only. There is deliberately no column that could hold a message
body (WA-NFR-6); a test asserts it. Conversation ``context`` holds counters and flags, never text.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, String, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base

CONVERSATION_STATES = ("new", "consent_pending", "declined", "idle", "human", "emergency")
MESSAGE_STATUSES = ("received", "sent", "delivered", "read", "failed", "body_lost")


class WhatsAppContact(Base):
    """One WhatsApp identity. ``last_inbound_at`` drives the 24-hour service-window check."""
    __tablename__ = "whatsapp_contacts"
    __table_args__ = (
        CheckConstraint(r"phone_e164 ~ '^\+[1-9][0-9]{7,14}$'", name="ck_whatsapp_contacts_phone_e164"),
        CheckConstraint("language IN ('en', 'pcm')", name="ck_whatsapp_contacts_language"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wa_id = Column(String, nullable=False, unique=True)
    phone_e164 = Column(String, nullable=False, unique=True)
    # NULL while an existing app account with the same number is waiting for a verified link.
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    display_name = Column(String, nullable=True)
    language = Column(String, nullable=False, default="en", server_default="en")
    source = Column(String, nullable=True)  # direct | ctwa | ref:<code>
    opted_in_at = Column(DateTime, nullable=True)
    marketing_opt_in = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    opted_out_at = Column(DateTime, nullable=True)
    last_inbound_at = Column(DateTime, nullable=True)
    blocked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WhatsAppConversation(Base):
    """Conversation state, one row per contact. Separate from any booking lifecycle."""
    __tablename__ = "whatsapp_conversations"
    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in CONVERSATION_STATES) + ")",
            name="ck_whatsapp_conversations_state",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("whatsapp_contacts.id"), nullable=False, unique=True)
    state = Column(String, nullable=False, default="new", server_default="new")
    context = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    assigned_agent_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    automation_paused = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class WhatsAppMessage(Base):
    """Metadata about one message in either direction. No body, ever."""
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_whatsapp_messages_direction"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in MESSAGE_STATUSES) + ")",
            name="ck_whatsapp_messages_status",
        ),
        Index("ix_whatsapp_messages_contact_created", "contact_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("whatsapp_contacts.id"), nullable=False)
    direction = Column(String, nullable=False)
    # Unique: the dedupe key. NULL for an outbound send that failed before Meta gave it an id.
    meta_message_id = Column(String, nullable=True, unique=True)
    type = Column(String, nullable=False)  # text | interactive | template | location | image | ...
    template_name = Column(String, nullable=True)
    category = Column(String, nullable=True)  # template category: utility | authentication | marketing
    status = Column(String, nullable=False)
    error_code = Column(String, nullable=True)
    cost_category = Column(String, nullable=True)  # Meta's pricing category from the status callback
    processed_at = Column(DateTime, nullable=True)  # inbound only: set when the engine has handled it
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
