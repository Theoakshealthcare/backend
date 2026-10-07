import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class CaregiverDropout(Base):
    """A caregiver cancelled a visit they had accepted (before it started). One row per drop-out, so the admin
    console can show a caregiver's record and count them; a booking can have several (one per caregiver who drops)."""
    __tablename__ = "caregiver_dropouts"
    __table_args__ = (Index("ix_caregiver_dropouts_caregiver", "caregiver_id", "dropped_at"),
                      Index("ix_caregiver_dropouts_booking", "booking_id"))

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    dropped_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    minutes_before_start = Column(Integer, nullable=False)  # negative: after the start time
    was_en_route = Column(Boolean, nullable=False, default=False)
