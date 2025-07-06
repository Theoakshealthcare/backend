import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean, Column, DateTime, String, Text, ForeignKey, Float, Integer, Enum as SQLEnum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

class PrescriptionStatus(str, Enum):
    PENDING = "pending"
    AVAILABLE = "available"
    PAID = "paid"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"

class Prescription(Base):
    __tablename__ = "prescriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    requested_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # Prescription Details
    medication_name = Column(String, nullable=False)
    dosage = Column(String, nullable=True)
    frequency = Column(String, nullable=True)
    duration = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    additional_info = Column(Text, nullable=True)
    doctor_note_path = Column(String, nullable=True)

    # Delivery
    delivery_option = Column(String, nullable=False)  # e.g., 'Home Delivery', 'Pickup'
    address = Column(Text, nullable=False)

    # Status
    status = Column(SQLEnum(PrescriptionStatus), default=PrescriptionStatus.PENDING)
    payment_status = Column(String, default="pending")

    # Payment
    total_cost = Column(Float, nullable=True)
    paystack_reference = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    patient = relationship("Patient")
    requested_by_user = relationship("User") 