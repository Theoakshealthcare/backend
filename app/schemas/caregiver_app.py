from datetime import datetime
from typing import List, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.booking import BookingStatus


class AvailabilityRequest(BaseModel):
    is_available: bool = Field(strict=True)


class AvailabilityResponse(BaseModel):
    is_available: bool


class HomeVisit(BaseModel):
    """A visit assigned to the caregiver (accepted, en_route or in_progress)."""
    id: uuid.UUID
    booking_code: str
    service_type: str
    status: BookingStatus
    dispatch_state: Optional[str] = None
    scheduled_date: datetime
    scheduled_end_date: datetime
    duration_hours: int
    patient_name: Optional[str] = None
    patient_address: Optional[str] = None
    patient_city: str
    patient_state: str


class HomeOffer(BaseModel):
    """A visit request not yet accepted. Deliberately carries no patient details."""
    booking_id: uuid.UUID
    service_type: str
    scheduled_date: datetime
    scheduled_end_date: datetime
    duration_hours: int
    area: str
    state: str
    offered_at: datetime


class CaregiverHome(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    profile_status: str
    is_available: bool
    location_updated_at: Optional[datetime] = Field(
        None, description="When the caregiver's position was last reported; null if never.")
    today_visits: List[HomeVisit]
    upcoming_visits: List[HomeVisit]
    open_offers: List[HomeOffer]
    wallet_balance_naira: float = Field(description="Wallet balance in NAIRA (the /caregiver/wallet endpoint is kobo).")
    can_author_care_plans: bool = Field(
        description="True for a caregiver with the admin-assigned nurse skill: show the Care plan entry points. The skill is "
                    "not visible anywhere else in the app.")
    currency: str
