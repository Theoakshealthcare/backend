import uuid
from datetime import datetime, date
from typing import Optional, List

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, Index, String, Text, ForeignKey, Date, Integer, JSON, UniqueConstraint, text
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
    __table_args__ = (CheckConstraint("balance >= 0", name="ck_caregiver_wallets_balance_not_negative"),)
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False, unique=True)
    balance = Column(Integer, default=0)  # kobo; the AVAILABLE balance (paid out weekly). Pending earnings are not in it.
    # Set when Paystack's word contradicted a payout (see app/services/payouts.py). While set, the caregiver is left out of new
    # batches and no automatic code returns money to the balance; an admin lifts it (``unfreeze``). It lives on the wallet
    # because the wallet row is the lock every money move already takes.
    payouts_frozen_at = Column(DateTime, nullable=True)
    payouts_frozen_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    caregiver = relationship("Caregiver", backref="wallet")

# Transaction types and statuses. An ``earning`` or ``late_cancel_compensation`` starts ``pending_clearance`` (credited,
# not yet withdrawable), becomes ``successful`` when the release job moves it to the wallet balance, and can be put on
# ``held`` or ``voided`` by an admin before that. A legacy ``withdrawal`` is ``pending`` -> ``successful`` | ``failed``.
TX_EARNING = "earning"
TX_LATE_CANCEL = "late_cancel_compensation"
TX_WITHDRAWAL = "withdrawal"  # legacy self-withdrawals (the endpoint is gone); old rows stay
TX_PAYOUT = "payout"          # a scheduled bank payout: see app/db/models/payout.py
EARNING_TYPES = (TX_EARNING, TX_LATE_CANCEL)
TX_PENDING_CLEARANCE, TX_HELD, TX_VOIDED, TX_SUCCESSFUL = "pending_clearance", "held", "voided", "successful"
# A ``payout`` is pending_approval (reserved, awaiting an admin) -> pending (approved, transfer in flight) -> successful, or
# failed / cancelled (the money is back in the balance).
TX_PENDING_APPROVAL, TX_PENDING, TX_FAILED, TX_CANCELLED = "pending_approval", "pending", "failed", "cancelled"


class CaregiverTransaction(Base):
    __tablename__ = "caregiver_transactions"
    __table_args__ = (
        # One earning and one compensation per booking, whatever the callers do: a double complete can never double-credit.
        # NULLs are distinct, so withdrawals (no booking) are not affected.
        UniqueConstraint("booking_id", "type", name="uq_caregiver_transactions_booking_type"),
        CheckConstraint("type IN ('earning','late_cancel_compensation','withdrawal','payout')", name="ck_caregiver_transactions_type"),
        CheckConstraint("status IN ('pending','successful','failed','pending_clearance','held','voided','pending_approval','cancelled')",
                        name="ck_caregiver_transactions_status"),
        Index("ix_caregiver_transactions_release", "available_at", postgresql_where=text("status = 'pending_clearance'")),
        Index("ix_caregiver_transactions_caregiver", "caregiver_id", "created_at"),
    )
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    type = Column(String, nullable=False)  # earning | late_cancel_compensation | withdrawal | payout
    amount = Column(Integer, nullable=False)  # Store in kobo
    status = Column(String, nullable=False, default='pending')
    bank_name = Column(String, nullable=True)
    account_number = Column(String, nullable=True)
    account_name = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    narration = Column(String, nullable=True)
    # Earnings only: the booking, what the share was taken of and at what percent (kept so the amount can be re-derived),
    # when it becomes withdrawable and when it did, and who last held or voided it and why.
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    base_amount_kobo = Column(BigInteger, nullable=True)
    share_percent = Column(Integer, nullable=True)
    available_at = Column(DateTime, nullable=True)
    released_at = Column(DateTime, nullable=True)
    status_reason = Column(Text, nullable=True)
    status_changed_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    status_changed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    caregiver = relationship("Caregiver", backref="transactions")

class CaregiverBankAccount(Base):
    __tablename__ = "caregiver_bank_accounts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False, unique=True)
    bank_name = Column(String, nullable=False)
    account_number = Column(String, nullable=False)
    account_name = Column(String, nullable=False)  # the name the bank returned for the account (Paystack resolve)
    # Payout destination (A3 slice 2). ``recipient_code`` is set only after the account was resolved and registered as a
    # Paystack transfer recipient: without it the caregiver is not paid. A change of account blocks payouts for 24 hours.
    bank_code = Column(String, nullable=True)
    recipient_code = Column(String, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    payout_blocked_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    caregiver = relationship("Caregiver", backref="bank_account")
