from datetime import date, datetime
from typing import Any, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.caregiver import CaregiverProfile

ServiceType = Literal["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"]


def _non_blank(value: Optional[str]) -> Optional[str]:
    if value is not None:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentDecision(_Strict):
    decision: Literal["verified", "rejected"]
    reason: Optional[str] = Field(default=None, max_length=2000)
    expires_on: Optional[date] = None

    _reason = field_validator("reason")(_non_blank)

    def model_post_init(self, __context) -> None:
        if self.decision == "rejected" and not self.reason:
            raise ValueError("reason is required when rejecting a document")


class IdentityDecision(_Strict):
    decision: Literal["verified", "failed"]
    notes: str = Field(max_length=4000)

    _notes = field_validator("notes")(_non_blank)


class BackgroundCheckDecision(_Strict):
    decision: Literal["cleared", "failed"]
    notes: str = Field(max_length=4000)
    provider_reference: Optional[str] = Field(default=None, max_length=200)
    document_id: Optional[UUID] = None

    _notes = field_validator("notes")(_non_blank)


class SkillIn(_Strict):
    service_type: ServiceType
    sub_specialty: Optional[str] = Field(default=None, max_length=100)

    _sub = field_validator("sub_specialty")(_non_blank)


class SkillsIn(_Strict):
    skills: List[SkillIn] = Field(max_length=50)


class ReasonIn(_Strict):
    reason: str = Field(max_length=2000)

    _reason = field_validator("reason")(_non_blank)


class RequestChangesIn(_Strict):
    items: List[str] = Field(min_length=1, max_length=50)
    note: str = Field(max_length=2000)

    _note = field_validator("note")(_non_blank)


# --- responses (GET /admin/caregivers, GET /admin/caregivers/{id}) -------------------------------
# These describe the shapes the console already receives; they add no fields and drop none.


class ApplicationListItem(BaseModel):
    id: UUID
    full_name: str
    state: Optional[str] = None
    specialization: Optional[str] = None
    years_of_experience: Optional[int] = None
    submitted_at: Optional[datetime] = None
    age_in_days: Optional[int] = None
    document_count: int
    identity_verification_status: str
    background_check_status: str
    profile_status: str


class ApplicationList(BaseModel):
    items: List[ApplicationListItem]
    total: int
    limit: int
    offset: int


class ConsentsOut(BaseModel):
    terms_accepted: bool
    privacy_policy_accepted: bool
    background_check_consent: bool
    consent_date: Optional[datetime] = None


class ApplicationDocument(BaseModel):
    id: UUID
    document_type: str
    document_name: str
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    verification_status: str
    rejection_reason: Optional[str] = None
    expires_on: Optional[date] = None
    uploaded_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    reviewed_by: Optional[UUID] = None
    download_url: str


class IdentityOut(BaseModel):
    status: str
    document_verification_result: Optional[Any] = None
    face_verification_status: Optional[str] = None
    face_verification_result: Optional[Any] = None


class BackgroundCheckOut(BaseModel):
    status: str
    evidence: Optional[Any] = None
    decided_by: Optional[UUID] = None
    decided_at: Optional[datetime] = None


class DecisionOut(BaseModel):
    approved_by: Optional[UUID] = None
    approved_at: Optional[datetime] = None
    suspended_at: Optional[datetime] = None
    suspension_reason: Optional[str] = None
    rejection_reason: Optional[str] = None
    changes_requested: Optional[Any] = None


class SkillOut(BaseModel):
    service_type: str
    sub_specialty: Optional[str] = None


class AuditEntry(BaseModel):
    id: UUID
    action: str
    admin_user_id: UUID
    entity_type: str
    entity_id: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime


class ApplicationDetail(BaseModel):
    profile: CaregiverProfile
    consents: ConsentsOut
    documents: List[ApplicationDocument]
    identity: IdentityOut
    background_check: BackgroundCheckOut
    decision: DecisionOut
    skills: List[SkillOut]
    audit_history: List[AuditEntry]
