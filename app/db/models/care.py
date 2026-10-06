"""Who a visit is for, and who may act for them (PRD section 10).

A person booking for themselves gets a recipient row that points back at them, so every booking has
a recipient and the self case needs no special path later.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class CareRecipient(Base):
    __tablename__ = "care_recipients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name = Column(String, nullable=False)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(String, nullable=True)
    phone_e164 = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    notes_encrypted = Column(Text, nullable=True)  # app.core.security.encrypt(); never plaintext (WA-NFR-7)
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CareRelationship(Base):
    __tablename__ = "care_relationships"
    __table_args__ = (
        UniqueConstraint("contact_user_id", "recipient_id", name="uq_care_relationships_user_recipient"),
        CheckConstraint("role IN ('self', 'family', 'guardian')", name="ck_care_relationships_role"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = Column(UUID(as_uuid=True), ForeignKey("care_recipients.id"), nullable=False, index=True)
    role = Column(String, nullable=False)
    clinical_access = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
