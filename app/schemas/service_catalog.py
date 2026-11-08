"""Service catalogue: the patient-facing list and the admin read/write models. No prices anywhere in here."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

MINUTES_PER_HOUR = 60
MAX_MINUTES = 1440
# Until bookings are priced and stored in minutes (the next slice) a booking is a whole number of hours, so the
# catalogue may only describe whole hours. That slice relaxes the step.
ALLOWED_STEP_MINUTES = (60,)
ALLOWED_MODES = ("home",)  # 'video' and 'both' exist in the table but are refused until a video provider exists


class BookableServiceOut(BaseModel):
    service_type: str = Field(examples=["nurse"])
    display_name: str = Field(examples=["Nurse visit"])
    mode: str = Field(examples=["home"])
    min_minutes: int = Field(examples=[60])
    step_minutes: int = Field(examples=[60])
    max_minutes: int = Field(examples=[1440])


class BookableServicesOut(BaseModel):
    services: list[BookableServiceOut]


class ServiceCatalogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    service_type: str
    enabled: bool
    display_name: str
    mode: str
    min_minutes: int
    step_minutes: int
    max_minutes: int
    sort_order: int
    priced: bool = Field(description="Does the newest pricing have a multiplier for it? A service can only be enabled when true")
    bookable: bool = Field(description="enabled AND priced: what patients are offered")
    updated_at: datetime
    updated_by: Optional[UUID] = None


class ServiceCatalogWrite(BaseModel):
    """The full editable state of one service (PUT replaces it: sending the same body twice changes nothing)."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    display_name: str = Field(min_length=1, max_length=60)
    mode: str = Field(description="'home' only for now")
    min_minutes: StrictInt
    step_minutes: StrictInt
    max_minutes: StrictInt
    sort_order: StrictInt = Field(ge=0, le=1000)
    reason: Optional[str] = Field(None, max_length=2000, description="Why (kept in the audit log)")

    @field_validator("display_name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("mode")
    @classmethod
    def _mode_home_only(cls, value: str) -> str:
        if value not in ALLOWED_MODES:
            raise ValueError("only 'home' is supported until video visits exist")
        return value

    @field_validator("reason")
    @classmethod
    def _reason_clean(cls, value: Optional[str]) -> Optional[str]:
        return (value.strip() or None) if value is not None else None

    @model_validator(mode="after")
    def _durations(self) -> "ServiceCatalogWrite":
        if self.step_minutes not in ALLOWED_STEP_MINUTES:
            raise ValueError("step_minutes must be 60 until half-hour bookings are supported")
        for name in ("min_minutes", "max_minutes"):
            value = getattr(self, name)
            if value % MINUTES_PER_HOUR or not MINUTES_PER_HOUR <= value <= MAX_MINUTES:
                raise ValueError(f"{name} must be a whole number of hours between 60 and {MAX_MINUTES} minutes")
        if self.min_minutes > self.max_minutes:
            raise ValueError("min_minutes must not exceed max_minutes")
        return self

    def changes(self) -> dict:
        return self.model_dump(exclude={"reason"})
