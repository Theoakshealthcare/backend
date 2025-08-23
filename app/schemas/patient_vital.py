from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel
import enum

class VitalSource(str, enum.Enum):
    manual = "manual"
    wearable = "wearable"

class PatientVitalBase(BaseModel):
    vital_type: str
    value: str
    unit: Optional[str] = None
    source: VitalSource = VitalSource.manual
    recorded_at: Optional[datetime] = None
    notes: Optional[str] = None

class PatientVitalCreate(PatientVitalBase):
    patient_id: UUID
    recorded_by_user_id: UUID

class PatientVitalUpdate(BaseModel):
    vital_type: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    source: Optional[VitalSource] = None
    recorded_at: Optional[datetime] = None
    notes: Optional[str] = None

class PatientVitalResponse(PatientVitalBase):
    id: UUID
    patient_id: UUID
    recorded_by_user_id: UUID
    recorded_at: datetime

    class Config:
        from_attributes = True 