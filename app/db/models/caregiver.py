import uuid
from datetime import datetime, date
from typing import Optional, List

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Index, String, Text, ForeignKey, Date, Integer, JSON, text
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
    address = Column(Text, nullable=True)
    emergency_contact_phone = Column(String, nullable=True)
    emergency_contact_name = Column(String, nullable=True)
    emergency_contact_address = Column(String, nullable=True)
    
    # Professional Information
    specialization = Column(String, nullable=True)
    years_of_experience = Column(Integer, nullable=True)
    preferred_work_hours = Column(String, nullable=True)  # JSON string
    service_areas = Column(ARRAY(String), nullable=True)
    
    # Verification Status
    profile_completion_step = Column(Integer, default=0)  # 0-3 for tracking progress
    identity_verification_status = Column(String, default='pending')  # pending, in_progress, verified, failed
    background_check_status = Column(String, default='pending')
    profile_status = Column(String, default='incomplete')  # incomplete, under_review, changes_requested, approved, rejected, suspended
    # background_check_status: pending, in_progress, cleared, failed (set only by an admin decision)

    # Admin approval workflow (written only by CaregiverApprovalService, never by the caregiver)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    suspended_at = Column(DateTime, nullable=True)
    suspension_reason = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    changes_requested = Column(JSON, nullable=True)  # {"items": [...], "note": "..."}
    background_check_evidence = Column(JSON, nullable=True)  # {"notes", "document_id", "provider_reference"}
    background_check_decided_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    background_check_decided_at = Column(DateTime, nullable=True)
    
    # Third-party Integration IDs
    onfido_applicant_id = Column(String, nullable=True)
    onfido_check_id = Column(String, nullable=True)
    
    # Consent and Legal
    terms_accepted = Column(Boolean, default=False)
    privacy_policy_accepted = Column(Boolean, default=False)
    background_check_consent = Column(Boolean, default=False)
    consent_date = Column(DateTime, nullable=True)
    
    # Prembly Verification Results
    document_verification_result = Column(JSON, nullable=True)
    face_verification_result = Column(JSON, nullable=True)
    face_verification_status = Column(String, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="caregiver", foreign_keys=[user_id])
    documents = relationship("CaregiverDocument", back_populates="caregiver")
    certifications = relationship("CaregiverCertification", back_populates="caregiver")
    bookings = relationship("Booking", back_populates="caregiver")
    notifications = relationship("BookingNotification", back_populates="caregiver")
    locations = relationship("CaregiverLocation", back_populates="caregiver")
    notes = relationship("BookingNote", back_populates="caregiver")
    skills = relationship("CaregiverSkill", back_populates="caregiver", cascade="all, delete-orphan")


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

    # Admin review
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    expires_on = Column(Date, nullable=True)

    # Relationship
    caregiver = relationship("Caregiver", back_populates="documents")


SERVICE_TYPES = ("nurse", "doctor", "physiotherapist", "caregiver", "nutritionist")


class CaregiverSkill(Base):
    """A structured service skill an admin assigned to a caregiver (used for matching)."""
    __tablename__ = "caregiver_skills"
    __table_args__ = (
        CheckConstraint(
            "service_type IN ('nurse','doctor','physiotherapist','caregiver','nutritionist')",
            name="ck_caregiver_skills_service_type",
        ),
        # sub_specialty is nullable, and NULLs are distinct in a plain unique constraint,
        # so the index coalesces it.
        Index(
            "uq_caregiver_skills_caregiver_service_sub",
            "caregiver_id", "service_type", text("coalesce(sub_specialty, '')"),
            unique=True,
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id", ondelete="CASCADE"), nullable=False)
    service_type = Column(String, nullable=False)
    sub_specialty = Column(String, nullable=True)
    created_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    caregiver = relationship("Caregiver", back_populates="skills")


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


class CaregiverWallet(Base):
    __tablename__ = "caregiver_wallets"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False, unique=True)
    balance = Column(Integer, default=0)  # Store in kobo for accuracy
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    caregiver = relationship("Caregiver", backref="wallet")

class CaregiverTransaction(Base):
    __tablename__ = "caregiver_transactions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    type = Column(String, nullable=False)  # 'earning' or 'withdrawal'
    amount = Column(Integer, nullable=False)  # Store in kobo
    status = Column(String, nullable=False, default='pending')  # 'pending', 'successful', 'failed'
    bank_name = Column(String, nullable=True)
    account_number = Column(String, nullable=True)
    account_name = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    narration = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    caregiver = relationship("Caregiver", backref="transactions")

class CaregiverBankAccount(Base):
    __tablename__ = "caregiver_bank_accounts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False, unique=True)
    bank_name = Column(String, nullable=False)
    account_number = Column(String, nullable=False)
    account_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    caregiver = relationship("Caregiver", backref="bank_account")
