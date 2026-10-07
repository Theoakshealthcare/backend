"""Pricing settings (``/booking-settings``): the admin write model and the response.

Every write publishes a new version, so the write model is strict: typed, bounded, no unknown fields.
"""

from datetime import datetime
from typing import Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.caregiver import SERVICE_TYPES
from app.services.pricing import MAX_MULTIPLIER

MAX_BASE_FEE = 1_000_000.0  # naira per hour; a typo in kobo (500000 for 5000) must not become a price
MAX_SURGE_MULTIPLIER = 5.0


class BookingSettingsWrite(BaseModel):
    """A complete pricing version plus why it is being published."""

    model_config = ConfigDict(extra="forbid")

    base_fee: float = Field(..., gt=0, le=MAX_BASE_FEE, allow_inf_nan=False, description="Base fee per hour, in naira")
    platform_fee_percent: float = Field(..., ge=0, le=100, allow_inf_nan=False, description="Platform fee in percent")
    multipliers: Optional[Dict[str, float]] = Field(
        None, description=f"Service type multipliers; keys: {', '.join(SERVICE_TYPES)}; each above 0 and at most {MAX_MULTIPLIER:g}")
    surge_pricing: bool = Field(False, description="Is surge pricing active?")
    surge_multiplier: float = Field(1.0, ge=1, le=MAX_SURGE_MULTIPLIER, allow_inf_nan=False, description="Surge multiplier")
    discount: float = Field(0.0, ge=0, le=100, allow_inf_nan=False, description="Discount in percent")
    reason: str = Field(..., max_length=2000, description="Why the pricing is changing (kept in the audit log)")

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("multipliers")
    @classmethod
    def _multipliers_known_and_bounded(cls, value: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
        if value is None:
            return value
        unknown = sorted(set(value) - set(SERVICE_TYPES))
        if unknown:
            raise ValueError(f"unknown service type(s): {', '.join(unknown)}")
        for key, multiplier in value.items():
            if not 0 < multiplier <= MAX_MULTIPLIER or multiplier != multiplier:
                raise ValueError(f"{key}: must be above 0 and at most {MAX_MULTIPLIER:g}")
        return value


# POST (first version / new version) and PUT (new version superseding {settings_id}) take the same body.
BookingSettingsCreate = BookingSettingsWrite
BookingSettingsUpdate = BookingSettingsWrite


class BookingSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base_fee: float = Field(..., description="Base fee per hour")
    platform_fee_percent: float = Field(..., description="Platform fee in percent")
    multipliers: Optional[Dict[str, float]] = Field(None, description="Service type multipliers")
    surge_pricing: Optional[bool] = Field(False, description="Is surge pricing active?")
    surge_multiplier: Optional[float] = Field(1.0, description="Surge multiplier")
    discount: Optional[float] = Field(0.0, description="Discount in percent")
    id: UUID
    created_at: datetime
    updated_at: datetime
