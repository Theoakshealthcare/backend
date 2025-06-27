from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, EmailStr, validator
import uuid

# Import patient schemas from the new patient.py file
from app.schemas.patient import Patient


# Base User Schemas
class UserBase(BaseModel):
    email: EmailStr
    phone_number: Optional[str] = None
    full_name: str
    user_type: Literal["patient", "caregiver"]


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


# Admin Schemas
class AdminBase(BaseModel):
    email: EmailStr
    full_name: str
    phone_number: Optional[str] = None
    permissions: Optional[str] = None


class AdminCreate(AdminBase):
    user_id: uuid.UUID


class Admin(AdminBase):
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
    token_type: str


class TokenPayload(BaseModel):
    sub: Optional[str] = None


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
