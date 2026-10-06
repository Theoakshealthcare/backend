"""Request and response models for admin auth and admin-user management (AD-FR-81..85)."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

PASSWORD_MIN_LENGTH = 12


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class LoginOut(BaseModel):
    """One of two shapes: ``mfa_required`` (already enrolled) or ``mfa_enrollment_required`` plus the
    secret to enrol with. Fields not in the shape are omitted from the response."""
    mfa_required: Optional[bool] = None
    mfa_enrollment_required: Optional[bool] = None
    mfa_token: str
    otpauth_uri: Optional[str] = None
    secret: Optional[str] = None


class MfaVerifyIn(BaseModel):
    mfa_token: str = Field(min_length=1, max_length=2048)
    code: str = Field(min_length=1, max_length=16)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class InviteAcceptIn(BaseModel):
    invite_token: str = Field(min_length=1, max_length=512)
    password: str = Field(max_length=256)

    @field_validator("password")
    @classmethod
    def _password_shape(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if len(v.encode()) > 72:
            raise ValueError("Password must be at most 72 bytes")
        return v


class InviteAcceptOut(BaseModel):
    accepted: bool = True
    email: EmailStr


class AdminOut(BaseModel):
    """The signed-in admin: returned by MFA verify and GET /admin/me."""
    id: UUID
    email: str
    full_name: str
    roles: List[str]
    permissions: List[str]


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class MfaVerifyOut(TokenPairOut):
    admin: AdminOut


class AdminInviteIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    roles: List[str] = Field(min_length=1, max_length=10)


class AdminListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    status: Literal["invited", "active", "disabled"]
    mfa_enabled: bool
    roles: List[str]
    last_login_at: Optional[datetime] = None
    locked_until: Optional[datetime] = None
    created_at: datetime


class AdminInviteOut(AdminListItem):
    invite_token: str
    invite_expires_at: datetime


class AdminPatchIn(BaseModel):
    roles: Optional[List[str]] = Field(default=None, min_length=1, max_length=10)
    status: Optional[Literal["active", "disabled"]] = None


class AuditEntryOut(BaseModel):
    id: UUID
    admin_user_id: UUID
    admin_email: Optional[str] = None
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime


class AuditPageOut(BaseModel):
    items: List[AuditEntryOut]
    total: int
    limit: int
    offset: int
