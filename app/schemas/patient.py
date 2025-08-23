from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, EmailStr, validator
import uuid


# Base Patient Schemas
class PatientBase(BaseModel):
    email: EmailStr
    full_name: str
    phone_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relationship: Optional[str] = None
    emergency_contact_address: Optional[str] = None
    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    medical_conditions: Optional[str] = None
    medications: Optional[str] = None
    preferred_language: Optional[str] = None
    special_requirements: Optional[str] = None

    @validator('gender')
    def validate_gender(cls, v):
        if v and v not in ['male', 'female', 'other']:
            raise ValueError('gender must be either "male", "female", or "other"')
        return v


class PatientCreate(PatientBase):
    user_id: uuid.UUID


class PatientUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relationship: Optional[str] = None
    emergency_contact_address: Optional[str] = None
    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    medical_conditions: Optional[str] = None
    medications: Optional[str] = None
    preferred_language: Optional[str] = None
    special_requirements: Optional[str] = None
    is_active: Optional[bool] = None
    profile_completion_step: Optional[int] = None

    @validator('gender')
    def validate_gender(cls, v):
        if v and v not in ['male', 'female', 'other']:
            raise ValueError('gender must be either "male", "female", or "other"')
        return v

    @validator('profile_completion_step')
    def validate_profile_completion_step(cls, v):
        if v is not None and (v < 0 or v > 3):
            raise ValueError('profile_completion_step must be between 0 and 3')
        return v


class Patient(PatientBase):
    id: uuid.UUID
    user_id: uuid.UUID
    is_active: bool
    profile_completion_step: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Patient Medical Record Schemas
class PatientMedicalRecordBase(BaseModel):
    record_type: str
    title: str
    description: Optional[str] = None
    date_recorded: Optional[date] = None
    doctor_name: Optional[str] = None
    hospital_clinic: Optional[str] = None
    severity: Optional[str] = None
    file_path: Optional[str] = None
    file_name: Optional[str] = None

    @validator('record_type')
    def validate_record_type(cls, v):
        valid_types = ['diagnosis', 'medication', 'allergy', 'procedure']
        if v not in valid_types:
            raise ValueError(f'record_type must be one of {valid_types}')
        return v

    @validator('severity')
    def validate_severity(cls, v):
        if v and v not in ['low', 'medium', 'high']:
            raise ValueError('severity must be either "low", "medium", or "high"')
        return v


class PatientMedicalRecordCreate(PatientMedicalRecordBase):
    patient_id: uuid.UUID


class PatientMedicalRecordUpdate(BaseModel):
    record_type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    date_recorded: Optional[date] = None
    doctor_name: Optional[str] = None
    hospital_clinic: Optional[str] = None
    severity: Optional[str] = None
    file_path: Optional[str] = None
    file_name: Optional[str] = None

    @validator('record_type')
    def validate_record_type(cls, v):
        if v:
            valid_types = ['diagnosis', 'medication', 'allergy', 'procedure']
            if v not in valid_types:
                raise ValueError(f'record_type must be one of {valid_types}')
        return v

    @validator('severity')
    def validate_severity(cls, v):
        if v and v not in ['low', 'medium', 'high']:
            raise ValueError('severity must be either "low", "medium", or "high"')
        return v


class PatientMedicalRecord(PatientMedicalRecordBase):
    id: uuid.UUID
    patient_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Patient Family Member Schemas
class PatientFamilyMemberBase(BaseModel):
    full_name: str
    relation_type: str
    phone_number: Optional[str] = None
    email: Optional[EmailStr] = None
    is_emergency_contact: bool = False
    can_make_medical_decisions: bool = False

    @validator('relation_type')
    def validate_relation_type(cls, v):
        valid_relationships = ['spouse', 'child', 'parent', 'sibling', 'grandparent', 'other']
        if v not in valid_relationships:
            raise ValueError(f'relation_type must be one of {valid_relationships}')
        return v


class PatientFamilyMemberCreate(PatientFamilyMemberBase):
    patient_id: uuid.UUID


class PatientFamilyMemberUpdate(BaseModel):
    full_name: Optional[str] = None
    relation_type: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[EmailStr] = None
    is_emergency_contact: Optional[bool] = None
    can_make_medical_decisions: Optional[bool] = None

    @validator('relation_type')
    def validate_relation_type(cls, v):
        if v:
            valid_relationships = ['spouse', 'child', 'parent', 'sibling', 'grandparent', 'other']
            if v not in valid_relationships:
                raise ValueError(f'relation_type must be one of {valid_relationships}')
        return v


class PatientFamilyMember(PatientFamilyMemberBase):
    id: uuid.UUID
    patient_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Comprehensive Patient Response (with related data)
class PatientWithDetails(Patient):
    medical_records: List[PatientMedicalRecord] = []
    family_members: List[PatientFamilyMember] = []


# Patient Profile Completion
class PatientProfileCompletion(BaseModel):
    profile_completion_step: int
    missing_fields: List[str] = []

    @validator('profile_completion_step')
    def validate_profile_completion_step(cls, v):
        if v < 0 or v > 3:
            raise ValueError('profile_completion_step must be between 0 and 3')
        return v 
    

# Patient Wallet Schemas
class PatientWalletSummary(BaseModel):
    balance: Decimal
    total_deposited: Decimal
    total_spent: Decimal
    total_refunded: Decimal
    currency: str = "NGN"

    class Config:
        from_attributes = True


# Patient Transaction Schemas
class PatientTransactionOut(BaseModel):
    id: str
    type: str
    amount: Decimal
    status: str
    paystack_reference: Optional[str] = None
    payment_method: Optional[str] = None
    description: Optional[str] = None
    booking_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PatientDepositRequest(BaseModel):
    amount: Decimal  # Amount in Naira (will be converted to kobo for Paystack)
    description: Optional[str] = None

    @validator('amount')
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError('Amount must be greater than 0')
        if v > 1000000:  # 1 million Naira limit
            raise ValueError('Amount cannot exceed 1,000,000 NGN')
        return v


class PatientDepositResponse(BaseModel):
    transaction_id: str
    paystack_reference: str
    authorization_url: str
    access_code: str
    amount: Decimal
    currency: str = "NGN"


class PatientPaymentRequest(BaseModel):
    amount: Decimal
    booking_id: Optional[uuid.UUID] = None
    description: Optional[str] = None

    @validator('amount')
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError('Amount must be greater than 0')
        return v


class PatientPaymentResponse(BaseModel):
    transaction_id: str
    status: str
    message: str
    remaining_balance: Decimal


# Refund Request Schemas
class PatientRefundRequestCreate(BaseModel):
    transaction_id: uuid.UUID
    amount: Decimal
    reason: str

    @validator('amount')
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError('Refund amount must be greater than 0')
        return v

    @validator('reason')
    def validate_reason(cls, v):
        if len(v.strip()) < 10:
            raise ValueError('Refund reason must be at least 10 characters long')
        return v.strip()


class PatientRefundRequestOut(BaseModel):
    id: str
    transaction_id: str
    amount: Decimal
    reason: str
    status: str
    admin_notes: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PatientRefundRequestResponse(BaseModel):
    request_id: str
    status: str
    message: str


# Paystack Webhook Schemas
class PaystackWebhookEvent(BaseModel):
    event: str
    data: dict


class PaystackTransactionData(BaseModel):
    id: int
    domain: str
    status: str
    reference: str
    amount: int  # Amount in kobo
    message: Optional[str] = None
    gateway_response: str
    paid_at: Optional[str] = None
    created_at: str
    channel: str
    currency: str
    ip_address: Optional[str] = None
    metadata: dict
    customer: dict
    authorization: dict


# Payment Verification Schema
class PaymentVerificationRequest(BaseModel):
    reference: str


class PaymentVerificationResponse(BaseModel):
    status: str
    message: str
    transaction_id: Optional[str] = None
    amount: Optional[Decimal] = None


# Admin Schemas for Refund Management
class AdminRefundAction(BaseModel):
    action: str  # 'approve' or 'reject'
    admin_notes: Optional[str] = None

    @validator('action')
    def validate_action(cls, v):
        if v not in ['approve', 'reject']:
            raise ValueError('Action must be either "approve" or "reject"')
        return v