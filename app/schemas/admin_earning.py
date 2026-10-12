"""Admin caregiver earnings (hold, release a hold, void): request and response models."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EarningStatusName = Literal["pending_clearance", "held", "successful", "voided"]
EarningTypeName = Literal["earning", "late_cancel_compensation"]


class EarningReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(max_length=2000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class EarningOut(BaseModel):
    id: UUID
    type: EarningTypeName
    status: EarningStatusName
    amount_kobo: int
    base_amount_kobo: Optional[int] = Field(description="What the share was taken of: the service fee, or the late fee kept")
    share_percent: Optional[int]
    caregiver_id: UUID
    caregiver_name: Optional[str]
    booking_id: Optional[UUID]
    booking_code: Optional[str]
    available_at: Optional[datetime]
    released_at: Optional[datetime]
    status_reason: Optional[str] = Field(description="Why it is held or voided")
    status_changed_by: Optional[UUID]
    status_changed_at: Optional[datetime]
    created_at: datetime


class EarningList(BaseModel):
    items: List[EarningOut]
    total: int
