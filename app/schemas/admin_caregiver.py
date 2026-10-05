from datetime import date
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
