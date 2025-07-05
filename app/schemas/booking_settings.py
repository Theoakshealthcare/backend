from pydantic import BaseModel, Field
from typing import Optional, Dict
from uuid import UUID
from datetime import datetime

class BookingSettingsBase(BaseModel):
    base_fee: float = Field(..., description="Base fee per hour")
    platform_fee_percent: float = Field(..., description="Platform fee in percent")
    multipliers: Optional[Dict[str, float]] = Field(None, description="Service type multipliers")
    surge_pricing: Optional[bool] = Field(False, description="Is surge pricing active?")
    surge_multiplier: Optional[float] = Field(1.0, description="Surge multiplier")
    discount: Optional[float] = Field(0.0, description="Discount in percent")

class BookingSettingsCreate(BookingSettingsBase):
    pass

class BookingSettingsUpdate(BookingSettingsBase):
    pass

class BookingSettingsResponse(BookingSettingsBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True 