import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PendingMediaPurge(Base):
    """A file at a media provider that should be deleted but could not be at the time (an erased account's
    profile image when Cloudinary was down). The database reference is already gone, so this row is the only
    record that the file exists. A purge job deletes it and sets ``purged_at``."""
    __tablename__ = "pending_media_purges"
    __table_args__ = (
        Index("ix_pending_media_purges_open", "created_at", postgresql_where=text("purged_at IS NULL")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String, nullable=False)  # cloudinary
    public_id = Column(String, nullable=False)
    reason = Column(String, nullable=False)  # account_erased
    user_id = Column(UUID(as_uuid=True), nullable=True)  # no FK: kept for the audit trail only
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    purged_at = Column(DateTime, nullable=True)
