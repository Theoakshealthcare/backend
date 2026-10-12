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


class VerificationStatusOut(BaseModel):
    profile_completion_step: Optional[int] = None
    identity_verification_status: Optional[str] = None
    background_check_status: Optional[str] = None
    profile_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    # {"items": [...], "note": "..."} while changes are requested, else null
    changes_requested: Optional[dict] = None


class FormOptionsOut(BaseModel):
    specializations: List[str]
    service_areas: List[str]
    work_hours: List[str]
    states: List[str]


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
    balance: int  # kobo; the available balance (paid out on the next payout day). Same number as available_balance
    total_earned: int  # kobo; earnings already released to the balance (pending ones are in pending_balance)
    total_withdrawn: int  # kobo; paid out to the bank account (scheduled payouts, and older self-withdrawals)
    currency: str = "NGN"
    available_balance: int  # kobo; cleared, and paid out on the next payout day if it reaches payout_min_kobo
    pending_balance: int  # kobo; earned but still inside the hold period; becomes available at next_available_at
    held_balance: int  # kobo; earned but on hold by support (for example after a no-show flag)
    total_pending: int  # kobo; pending_balance + held_balance
    next_available_at: Optional[datetime] = None  # when the earliest pending earning becomes available
    # Payouts (A3 slice 2). Caregivers do not withdraw: Oakly pays the available balance to the bank account on a schedule.
    payout_cadence: str = "weekly"  # weekly | biweekly | monthly
    payout_min_kobo: int = 0  # a balance below this waits for the next payout day
    next_payout_on: Optional[date] = None  # the next payout day (UTC date)
    payout_in_progress_kobo: int = 0  # taken out of the balance for a payout that is awaiting approval or on its way
    payouts_blocked_until: Optional[datetime] = None  # set for 24 hours after the bank account is changed


class CaregiverTransactionOut(BaseModel):
    id: str
    type: Literal['earning', 'late_cancel_compensation', 'withdrawal', 'payout']
    amount: int  # in kobo
    status: str  # earnings: pending_clearance | held | successful | voided; payouts: pending_approval | pending | successful | failed | cancelled
    booking_id: Optional[str] = None
    available_at: Optional[datetime] = None  # earnings: when it becomes withdrawable (a UTC time)
    released_at: Optional[datetime] = None
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_name: Optional[str] = None
    reference: Optional[str] = None
    narration: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BankOut(BaseModel):
    name: str
    code: str


class BankAccountResolveRequest(BaseModel):
    account_number: str = Field(min_length=1, max_length=20)
    bank_code: str = Field(min_length=1, max_length=20)


class BankAccountResolveOut(BaseModel):
    account_name: str  # the name the bank holds for the account (Paystack)
    account_number: str
    bank_code: str
    bank_name: str


class CaregiverBankAccountOut(BaseModel):
    bank_name: str
    bank_code: Optional[str] = None  # null on an account saved before verified payouts: save it again to be paid
    account_number: str
    account_name: str
    verified: bool  # true once the name was resolved by the bank and the account registered for payouts
    payouts_blocked_until: Optional[datetime] = None  # a changed account is not paid for 24 hours
    created_at: datetime
    updated_at: datetime

class CaregiverBankAccountUpdate(BaseModel):
    bank_code: str = Field(min_length=1, max_length=20)  # from GET /caregiver/banks
    account_number: str = Field(min_length=1, max_length=20)
