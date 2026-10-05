import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PushToken(Base):
    """An Expo push token for one device. The token is unique: a device that signs in as someone
    else is re-pointed at the new user, so it never receives the previous user's pushes."""
    __tablename__ = "push_tokens"
    __table_args__ = (
        CheckConstraint("platform IN ('ios','android')", name="ck_push_tokens_platform"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String, nullable=False, unique=True)
    platform = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
