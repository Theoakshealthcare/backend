import re
from datetime import datetime, date
from typing import Optional, Literal
from urllib.parse import urlparse
from pydantic import BaseModel, EmailStr, validator, field_validator, Field, model_validator
from pydantic_core import PydanticCustomError
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


class DetailResponse(BaseModel):
    detail: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordResponse(Token):
    """The caller's replacement tokens: every token issued before the change is dead, this pair is the one to keep."""
    detail: str


class TokenPayload(BaseModel):
    sub: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordReset(BaseModel):
    """``{email, code, new_password}`` (the emailed 5-digit code) or, deprecated, ``{token, new_password}`` (the long
    link token the shipped caregiver app pastes). Exactly one form."""
    new_password: str
    email: Optional[EmailStr] = None
    code: Optional[str] = None
    token: Optional[str] = None  # deprecated

    @model_validator(mode="after")
    def _exactly_one_form(self) -> "PasswordReset":
        by_code = self.email is not None and self.code is not None
        if self.token is not None:
            if self.email is not None or self.code is not None:
                raise ValueError("send either token, or email and code, not both")
        elif not by_code:
            raise ValueError("email and code are required")
        return self


class EmailVerificationRequest(BaseModel):
    email: EmailStr


class EmailVerification(BaseModel):
    email: EmailStr
    code: str


# Combined User Response (with type-specific data)
class UserWithTypeData(User):
    patient: Optional[Patient] = None
    caregiver: Optional[Caregiver] = None


PROFILE_NAME_MAX = 120
PROFILE_URL_MAX = 2048
_PHONE_SEPARATORS = re.compile(r"[\s().-]")
_PHONE = re.compile(r"\+?[0-9]{7,15}")


def _profile_error(message: str) -> PydanticCustomError:
    return PydanticCustomError("profile_invalid", "{message}", {"message": message})


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

    # name, phone and photo: left out = unchanged; an explicit null clears phone and photo (a name cannot be cleared)
    @field_validator("full_name")
    @classmethod
    def _full_name(cls, v):
        if v is None:
            raise _profile_error("Full name can't be removed. Send a new name or leave it out.")
        v = v.strip()
        if not v:
            raise _profile_error("Full name can't be blank.")
        if len(v) > PROFILE_NAME_MAX:
            raise _profile_error(f"Full name can be at most {PROFILE_NAME_MAX} characters.")
        return v

    @field_validator("phone_number")
    @classmethod
    def _phone_number(cls, v):
        if v is None:
            return None
        digits = _PHONE_SEPARATORS.sub("", v)
        if not _PHONE.fullmatch(digits):
            raise _profile_error("Phone number must be 7 to 15 digits, for example +2348012345678. Send null to remove it.")
        return digits

    @field_validator("profile_image_url")
    @classmethod
    def _profile_image_url(cls, v):
        if v is None:
            return None
        v = v.strip()
        parts = urlparse(v) if v and not re.search(r"\s", v) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.netloc or len(v) > PROFILE_URL_MAX:
            raise _profile_error(f"Photo link must be an http or https URL of at most {PROFILE_URL_MAX} characters. Send null to remove it.")
        return v


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
