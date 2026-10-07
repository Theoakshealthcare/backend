"""Admin booking policy (cancellation tiers and caregiver drop-out timings): request and response models."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

POLICY_FIELDS = (
    "cancel_free_hours", "cancel_late_hours", "cancel_fee_percent", "cancel_late_fee_percent",
    "dropout_redispatch_min_lead_minutes", "dropout_decision_lead_minutes", "onmyway_compensation_window_minutes",
    "caregiver_late_fee_share_percent",
)


class BookingPolicyOut(BaseModel):
    cancel_free_hours: int = Field(description="Full refund when the visit is more than this many hours away (or nobody is assigned)")
    cancel_late_hours: int = Field(description="Inside this many hours of the start, or once the caregiver is on the way, the late fee applies")
    cancel_fee_percent: int = Field(description="Fee, as a percent of the service fee, between the two lines above")
    cancel_late_fee_percent: int = Field(description="Fee, as a percent of the service fee, inside the late window or once on the way")
    dropout_redispatch_min_lead_minutes: int = Field(description="A caregiver drop-out more than this many minutes before the start is re-dispatched automatically")
    dropout_decision_lead_minutes: int = Field(description="If nobody has accepted this many minutes before the start, the family is given the choice")
    onmyway_compensation_window_minutes: int = Field(description="An on-my-way tapped no earlier than this many minutes before the start can qualify for compensation")
    caregiver_late_fee_share_percent: int = Field(description="Caregiver's share of a late fee, in percent (stored now, paid out by the compensation work)")
    updated_at: Optional[datetime] = None
    updated_by: Optional[UUID] = None


class BookingPolicyPatch(BaseModel):
    """Send only the fields to change, and why. The merged policy is validated as a whole."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=2000)
    cancel_free_hours: Optional[int] = Field(default=None, strict=True)
    cancel_late_hours: Optional[int] = Field(default=None, strict=True)
    cancel_fee_percent: Optional[int] = Field(default=None, strict=True)
    cancel_late_fee_percent: Optional[int] = Field(default=None, strict=True)
    dropout_redispatch_min_lead_minutes: Optional[int] = Field(default=None, strict=True)
    dropout_decision_lead_minutes: Optional[int] = Field(default=None, strict=True)
    onmyway_compensation_window_minutes: Optional[int] = Field(default=None, strict=True)
    caregiver_late_fee_share_percent: Optional[int] = Field(default=None, strict=True)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _something_to_change(self) -> "BookingPolicyPatch":
        if not self.changes():
            raise ValueError("send at least one policy field to change")
        return self

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in POLICY_FIELDS if getattr(self, name) is not None}
