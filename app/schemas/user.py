from datetime import datetime, date
from typing import Optional, Literal
from pydantic import BaseModel, EmailStr, validator, Field
import uuid

# Import patient schemas from the new patient.py file
from app.schemas.patient import Patient
from uuid import UUID


# Base User Schemas
class UserBase(BaseModel):
    email: EmailStr
    phone_number: Optional[str] = None
    full_name: str
    user_type: Literal["patient", "caregiver"]
    profile_image_url: Optional[str] = None


class UserCreate(UserBase):
    password: str

    @validator('user_type')
    def validate_user_type(cls, v):
        if v not in ['patient', 'caregiver']:
            raise ValueError('user_type must be either "patient" or "caregiver"')
        return v


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = None
    full_name: Optional[str] = None


class UserInDBBase(UserBase):
    email: Optional[EmailStr] = None  # phone-first (WhatsApp) users have none; registration stays strict
    id: uuid.UUID
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class User(UserInDBBase):
    pass


class UserInDB(UserInDBBase):
    hashed_password: str


# Caregiver Schemas
class CaregiverBase(BaseModel):
    email: EmailStr
    full_name: str
    phone_number: Optional[str] = None


class CaregiverCreate(CaregiverBase):
    user_id: uuid.UUID


class CaregiverUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    phone_number: Optional[str] = None


class Caregiver(CaregiverBase):
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Authentication Schemas
class UserLogin(BaseModel):
    email_or_phone: str
    password: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class TokenPayload(BaseModel):
    sub: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordReset(BaseModel):
    token: str
    new_password: str


class EmailVerificationRequest(BaseModel):
    email: EmailStr


class EmailVerification(BaseModel):
    email: EmailStr
    code: str


# Combined User Response (with type-specific data)
class UserWithTypeData(User):
    patient: Optional[Patient] = None
    caregiver: Optional[Caregiver] = None


class UserProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = None
    profile_image_url: Optional[str] = None
    address: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[date] = None
    age: Optional[int] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_address: Optional[str] = None
    password: Optional[str] = None

class UserProfileResponse(BaseModel):
    id: UUID
    full_name: str
    email: Optional[EmailStr] = None  # phone-only (WhatsApp) users have none
    phone_number: Optional[str]
    profile_image_url: Optional[str]
    user_type: str
    # Patient/Caregiver specific fields
    address: Optional[str]
    gender: Optional[str]
    date_of_birth: Optional[date]
    emergency_contact_name: Optional[str]
    emergency_contact_phone: Optional[str]
    emergency_contact_address: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True
