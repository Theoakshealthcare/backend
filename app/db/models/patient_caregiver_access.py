import uuid
from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.db.base import Base

class PatientCaregiverAccess(Base):
    __tablename__ = "patient_caregiver_access"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    granted_by_patient = Column(Boolean, default=True, nullable=False)
    granted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True)

    patient = relationship("Patient")
    caregiver = relationship("Caregiver") 