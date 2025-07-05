import uuid
from datetime import datetime
from sqlalchemy import Column, DateTime, Float, String, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

class BookingSettings(Base):
    __tablename__ = "booking_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    base_fee = Column(Float, nullable=False, default=5000)  # Default base fee per hour
    platform_fee_percent = Column(Float, nullable=False, default=10.0)  # Platform fee in percent
    multipliers = Column(JSON, nullable=True)  # e.g. {"nurse": 1.0, "doctor": 1.5, ...}
    surge_pricing = Column(Boolean, default=False)
    surge_multiplier = Column(Float, nullable=True, default=1.0)
    discount = Column(Float, nullable=True, default=0.0)  # Discount in percent
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow) 