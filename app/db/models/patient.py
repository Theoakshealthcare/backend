import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, String, Text, ForeignKey, Date, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class Patient(Base):
    __tablename__ = "patients"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    
    # Basic Information
    email = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(String, nullable=True)  # 'male', 'female', 'other'
    
    # Address Information
    address = Column(Text, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    
    # Emergency Contact
    emergency_contact_name = Column(String, nullable=True)
    emergency_contact_phone = Column(String, nullable=True)
    emergency_contact_relationship = Column(String, nullable=True)
    
    # Medical Information
    blood_group = Column(String, nullable=True)
    allergies = Column(Text, nullable=True)
    medical_conditions = Column(Text, nullable=True)
    medications = Column(Text, nullable=True)
    
    # Preferences
    preferred_language = Column(String, nullable=True)
    special_requirements = Column(Text, nullable=True)
    
    # Status
    is_active = Column(Boolean, default=True)
    profile_completion_step = Column(Integer, default=0)  # 0-3 for tracking progress
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="patient")
    bookings = relationship("Booking", back_populates="patient")
    medical_records = relationship("PatientMedicalRecord", back_populates="patient_record")
    family_members = relationship("PatientFamilyMember", back_populates="patient_record")


class PatientMedicalRecord(Base):
    __tablename__ = "patient_medical_records"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    
    # Record Details
    record_type = Column(String, nullable=False)  # 'diagnosis', 'medication', 'allergy', 'procedure'
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    date_recorded = Column(Date, nullable=True)
    
    # Medical Details
    doctor_name = Column(String, nullable=True)
    hospital_clinic = Column(String, nullable=True)
    severity = Column(String, nullable=True)  # 'low', 'medium', 'high'
    
    # File Attachment
    file_path = Column(String, nullable=True)
    file_name = Column(String, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    patient_record = relationship("Patient", back_populates="medical_records")


class PatientFamilyMember(Base):
    __tablename__ = "patient_family_members"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    
    # Family Member Details
    full_name = Column(String, nullable=False)
    relation_type = Column(String, nullable=False)  # 'spouse', 'child', 'parent', 'sibling'
    phone_number = Column(String, nullable=True)
    email = Column(String, nullable=True)
    
    # Emergency Contact
    is_emergency_contact = Column(Boolean, default=False)
    can_make_medical_decisions = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    patient_record = relationship("Patient", back_populates="family_members") 