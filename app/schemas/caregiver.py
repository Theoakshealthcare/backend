from datetime import datetime, date
from typing import Optional, List, Literal
from pydantic import BaseModel, EmailStr, Field, validator
import uuid


# Caregiver Profile Schemas
class CaregiverProfileStep1(BaseModel):
    date_of_birth: date
    gender: Literal["male", "female", "other"]
    state: str
    address: str
    emergency_contact_phone: str


class CaregiverProfileStep2(BaseModel):
    specialization: str
    years_of_experience: int
    preferred_work_hours: str
    service_areas: List[str]


class CaregiverConsentData(BaseModel):
    terms_accepted: bool
    privacy_policy_accepted: bool
    background_check_consent: bool


# Document Upload Schemas
class DocumentUploadResponse(BaseModel):
    id: uuid.UUID
    document_type: str
    document_name: str
    file_path: str
    verification_status: str
    uploaded_at: datetime

    class Config:
        from_attributes = True


# Identity Verification Schemas
class IdentityVerificationRequest(BaseModel):
    document_type: Literal["passport", "driving_licence", "national_identity_card"]


class IdentityVerificationResponse(BaseModel):
    applicant_id: str
    sdk_token: str
    check_id: Optional[str] = None


class FaceVerificationRequest(BaseModel):
    applicant_id: str


# Complete Caregiver Profile Response
class CaregiverProfile(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: str
    phone_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    state: Optional[str] = None
    address: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    specialization: Optional[str] = None
    years_of_experience: Optional[int] = None
    preferred_work_hours: Optional[str] = None
    service_areas: Optional[List[str]] = None
    profile_completion_step: int
    identity_verification_status: str
    background_check_status: str
    profile_status: str
    terms_accepted: bool
    privacy_policy_accepted: bool
    background_check_consent: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Specialization and Service Area Options
class SpecializationOptions(BaseModel):
    specializations: List[str]
    service_areas: List[str]
    work_hours: List[str]
    states: List[str]


# Verification Result Schema
class VerificationResultRequest(BaseModel):
    type: Literal["document", "face"]
    result: dict


class CaregiverWalletSummary(BaseModel):
    balance: int  # in kobo
    total_earned: int  # in kobo
    total_withdrawn: int  # in kobo
    currency: str = "NGN"


class CaregiverTransactionOut(BaseModel):
    id: str
    type: Literal['earning', 'withdrawal']
    amount: int  # in kobo
    status: str
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_name: Optional[str] = None
    reference: Optional[str] = None
    narration: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CaregiverBankAccountOut(BaseModel):
    bank_name: str
    account_number: str
    account_name: str
    created_at: datetime
    updated_at: datetime

class CaregiverBankAccountUpdate(BaseModel):
    bank_name: str
    account_number: str
    account_name: str

class CaregiverWithdrawalRequest(BaseModel):
    amount: int = Field(gt=0)  # in kobo
    narration: Optional[str] = None

class CaregiverWithdrawalResponse(BaseModel):
    transaction_id: str
    status: str
    message: str
