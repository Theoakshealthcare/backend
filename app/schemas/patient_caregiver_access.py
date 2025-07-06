from datetime import datetime
from uuid import UUID
from typing import Optional
from pydantic import BaseModel

class PatientCaregiverAccessBase(BaseModel):
    patient_id: UUID
    caregiver_id: UUID

class PatientCaregiverAccessGrant(PatientCaregiverAccessBase):
    pass

class PatientCaregiverAccessRevoke(PatientCaregiverAccessBase):
    pass

class PatientCaregiverAccessResponse(PatientCaregiverAccessBase):
    id: UUID
    granted_by_patient: bool
    granted_at: datetime
    revoked_at: Optional[datetime] = None

    class Config:
        from_attributes = True 