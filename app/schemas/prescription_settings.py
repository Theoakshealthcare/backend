from datetime import datetime
from typing import Optional, Dict
from uuid import UUID
from pydantic import BaseModel

class PrescriptionSettingsBase(BaseModel):
    base_fee: float
    multipliers: Optional[Dict[str, float]] = None
    surge_pricing: Optional[bool] = False
    surge_multiplier: Optional[float] = 1.0
    discount: Optional[float] = 0.0

class PrescriptionSettingsCreate(PrescriptionSettingsBase):
    pass

class PrescriptionSettingsUpdate(PrescriptionSettingsBase):
    pass

class PrescriptionSettingsResponse(PrescriptionSettingsBase):
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True 