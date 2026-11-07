import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class Notification(Base):
    """One thing an app patient was told, shown in their in-app feed (and sent as a push when a device is registered).

    The text is written for a lock screen: it never holds a visit code, an address or anything about their health.
    ``data`` carries only what the app needs to open the right screen (``booking_id``, ``target``). ``event_id`` is the
    ``booking_events`` outbox row it came from; it is unique so a retried delivery cannot write a second row.
    """
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_notifications_event_id"),
        Index("ix_notifications_user_feed", "user_id", text("created_at DESC"), text("id DESC")),
        Index("ix_notifications_user_unread", "user_id", postgresql_where=text("read_at IS NULL")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    data = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    event_id = Column(UUID(as_uuid=True), nullable=True)
