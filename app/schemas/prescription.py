from typing import Optional
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel

class PrescriptionBase(BaseModel):
    medication_name: str
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    reason: Optional[str] = None
    additional_info: Optional[str] = None
    delivery_option: str
    address: str

class PrescriptionCreate(PrescriptionBase):
    patient_id: UUID
    requested_by_user_id: UUID
    doctor_note_path: Optional[str] = None
    total_cost: Optional[float] = None

class PrescriptionUpdate(BaseModel):
    status: Optional[str] = None
    payment_status: Optional[str] = None
    doctor_note_path: Optional[str] = None
    total_cost: Optional[float] = None
    paystack_reference: Optional[str] = None

class PrescriptionResponse(PrescriptionBase):
    id: UUID
    patient_id: UUID
    requested_by_user_id: UUID
    doctor_note_path: Optional[str]
    status: str
    payment_status: str
    total_cost: Optional[float]
    paystack_reference: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True 