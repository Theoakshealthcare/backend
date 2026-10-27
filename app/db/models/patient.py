import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Column, DateTime, String, Text, ForeignKey, Date, Integer, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class Patient(Base):
    __tablename__ = "patients"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    
    # Basic Information
    email = Column(String, nullable=True)  # NULL for phone-first (WhatsApp) patients
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
    emergency_contact_address = Column(String, nullable=True)
    
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
    wallet = relationship("PatientWallet", back_populates="patient", uselist=False)


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
    

class PatientWallet(Base):
    __tablename__ = "patient_wallets"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, unique=True)
    
    # Wallet Details
    # All amounts are integer kobo (BIGINT)
    balance_kobo = Column(BigInteger, default=0, nullable=False)  # Current available balance
    total_deposited_kobo = Column(BigInteger, default=0, nullable=False)  # Total amount deposited
    total_spent_kobo = Column(BigInteger, default=0, nullable=False)  # Total amount spent on services
    total_refunded_kobo = Column(BigInteger, default=0, nullable=False)  # Total amount refunded
    
    # Status
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    patient = relationship("Patient", back_populates="wallet")
    transactions = relationship("PatientTransaction", back_populates="wallet")


class PatientTransaction(Base):
    __tablename__ = "patient_transactions"
    __table_args__ = (
        Index('idx_patient_transactions_patient_id', 'patient_id'),
        Index('idx_patient_transactions_paystack_reference', 'paystack_reference'),
        Index('idx_patient_transactions_status', 'status'),
        Index('idx_patient_transactions_created_at', 'created_at'),
    )
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("patient_wallets.id"), nullable=False)
    
    # Transaction Details
    type = Column(String, nullable=False)  # 'deposit', 'payment', 'refund'
    amount_kobo = Column(BigInteger, nullable=False)  # integer kobo, sent to Paystack as-is
    status = Column(String, nullable=False)  # 'pending', 'successful', 'failed', 'cancelled'
    
    # Payment Gateway Details (Paystack)
    paystack_reference = Column(String, nullable=True, unique=True)  # Paystack transaction reference - must be unique
    paystack_status = Column(String, nullable=True)  # Paystack transaction status
    payment_method = Column(String, nullable=True)  # 'card', 'bank_transfer', 'ussd'
    
    # Transaction Meta
    description = Column(Text, nullable=True)  # Description of the transaction
    booking_id = Column(UUID(as_uuid=True), nullable=True)  # Related booking if payment for service
    
    # Refund Details (if type is refund)
    refund_reason = Column(Text, nullable=True)
    admin_approved = Column(Boolean, default=False)
    admin_id = Column(UUID(as_uuid=True), nullable=True)  # Admin who approved refund
    refund_id = Column(UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=True)  # the refund a wallet credit came from
    approved_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    patient = relationship("Patient")
    wallet = relationship("PatientWallet", back_populates="transactions")


class PatientRefundRequest(Base):
    __tablename__ = "patient_refund_requests"
    __table_args__ = (
        Index('idx_patient_refund_requests_patient_id', 'patient_id'),
        Index('idx_patient_refund_requests_status', 'status'),
        Index('idx_patient_refund_requests_created_at', 'created_at'),
    )
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    transaction_id = Column(UUID(as_uuid=True), ForeignKey("patient_transactions.id"), nullable=False)
    
    # Refund Details
    amount_kobo = Column(BigInteger, nullable=False)  # Amount to refund, integer kobo
    reason = Column(Text, nullable=False)  # Reason for refund request
    
    # Status
    status = Column(String, default="pending")  # 'pending', 'approved', 'rejected'
    
    # Admin Action
    admin_id = Column(UUID(as_uuid=True), nullable=True)  # Admin who processed the request
    admin_notes = Column(Text, nullable=True)  # Admin notes for approval/rejection
    processed_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    patient = relationship("Patient")
    transaction = relationship("PatientTransaction")