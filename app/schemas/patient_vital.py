from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, model_validator
from pydantic_core import PydanticCustomError
import enum

from app.services.vital_rules import VitalRuleError, check_notes, check_reading

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

    @model_validator(mode="after")
    def _plausible_reading(self):
        # Only new readings are checked (never the response model), so rows saved earlier still list.
        try:
            self.vital_type, self.value, self.unit = check_reading(self.vital_type, self.value, self.unit)
            check_notes(self.notes)
        except VitalRuleError as exc:
            raise PydanticCustomError("vital_invalid", "{message}", {"message": str(exc)})
        return self

class PatientVitalUpdate(BaseModel):
    vital_type: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    source: Optional[VitalSource] = None
    recorded_at: Optional[datetime] = None
    notes: Optional[str] = None

class VitalFlagOut(BaseModel):
    """The clinician-approved rating of one reading (``app/services/vital_flags.py``). Additive: older clients ignore it."""
    status: str  # normal | high | low | critical
    label: str  # Normal | High | Low | Critical
    reason: Optional[str] = None  # null for normal
    emergency: bool = False  # true only for critical: show `message`
    message: Optional[str] = None  # "This may be an emergency. Call 112 now." when emergency, else null


class PatientVitalResponse(PatientVitalBase):
    id: UUID
    patient_id: UUID
    recorded_by_user_id: UUID
    recorded_at: datetime
    # Additive. null for weight, sleep and any reading that cannot be rated (a value that is not a number).
    flag: Optional[VitalFlagOut] = None
    # Glucose only: random | fasting | after_meal, read from the label the app stored in `notes` (RBS / FBS / PP).
    glucose_context: Optional[str] = None

    class Config:
        from_attributes = True 