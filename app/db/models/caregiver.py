import uuid
from datetime import datetime, date
from typing import Optional, List

from sqlalchemy import Boolean, Column, DateTime, String, Text, ForeignKey, Date, Integer, JSON
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import relationship

from app.db.base import Base


class Caregiver(Base):
    __tablename__ = "caregivers"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    
    # Basic Information
    email = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(String, nullable=True)  # 'male', 'female', 'other'
    state = Column(String, nullable=True)
    house_address = Column(Text, nullable=True)
    emergency_contact_phone = Column(String, nullable=True)
    
    # Professional Information
    specialization = Column(String, nullable=True)
    years_of_experience = Column(Integer, nullable=True)
    preferred_work_hours = Column(String, nullable=True)  # JSON string
    service_areas = Column(ARRAY(String), nullable=True)
    
    # Verification Status
    profile_completion_step = Column(Integer, default=0)  # 0-3 for tracking progress
    identity_verification_status = Column(String, default='pending')  # pending, in_progress, verified, failed
    background_check_status = Column(String, default='pending')
    profile_status = Column(String, default='incomplete')  # incomplete, under_review, approved, rejected
    
    # Third-party Integration IDs
    onfido_applicant_id = Column(String, nullable=True)
    onfido_check_id = Column(String, nullable=True)
    
    # Consent and Legal
    terms_accepted = Column(Boolean, default=False)
    privacy_policy_accepted = Column(Boolean, default=False)
    background_check_consent = Column(Boolean, default=False)
    consent_date = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="caregiver")
    documents = relationship("CaregiverDocument", back_populates="caregiver")
    certifications = relationship("CaregiverCertification", back_populates="caregiver")
    bookings = relationship("Booking", back_populates="caregiver")
    notifications = relationship("BookingNotification", back_populates="caregiver")
    locations = relationship("CaregiverLocation", back_populates="caregiver")
    notes = relationship("BookingNote", back_populates="caregiver")


class CaregiverDocument(Base):
    __tablename__ = "caregiver_documents"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    
    document_type = Column(String, nullable=False)  # 'license', 'certification', 'id_document'
    document_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    file_size = Column(Integer, nullable=True)
    mime_type = Column(String, nullable=True)
    verification_status = Column(String, default='pending')  # pending, verified, rejected
    
    # Onfido document data
    onfido_document_id = Column(String, nullable=True)
    extraction_data = Column(JSON, nullable=True)  # OCR extracted data
    
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)

    # Relationship
    caregiver = relationship("Caregiver", back_populates="documents")


class CaregiverCertification(Base):
    __tablename__ = "caregiver_certifications"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    
    certification_name = Column(String, nullable=False)
    issuing_organization = Column(String, nullable=False)
    issue_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True)
    certification_number = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    verification_status = Column(String, default='pending')
    
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship
    caregiver = relationship("Caregiver", back_populates="certifications")
