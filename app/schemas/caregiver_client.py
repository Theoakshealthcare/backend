"""What a caregiver sees of the client on one of their visits. A deliberately small, read-only shape:
no address, coordinates, email, phone number, booking condition or special instructions, and no care
plan (care plans are not built). Every field is the caregiver's to see for as long as the visit is live."""

from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel


class ClientEmergencyContact(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    relationship: Optional[str] = None


class ClientMedicalRecord(BaseModel):
    record_type: str
    title: str
    description: Optional[str] = None
    severity: Optional[str] = None
    date_recorded: Optional[date] = None


class ClientMedicalHistory(BaseModel):
    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    medical_conditions: Optional[str] = None
    medications: Optional[str] = None
    records: List[ClientMedicalRecord] = []


class ClientVital(BaseModel):
    """The newest reading of one vital type. No status: the backend does not rate readings."""
    vital_type: str
    value: str
    unit: Optional[str] = None
    recorded_at: datetime


class CaregiverClientProfile(BaseModel):
    booking_id: UUID
    patient_id: UUID
    full_name: str
    profile_image_url: Optional[str] = None
    # "myself", or who the visit is for ("mother", "family", ...). When it is not "myself" the patient
    # record is the person who booked, so their medical history and photo are not shown as the client's.
    booking_for: str
    age: Optional[int] = None
    gender: Optional[str] = None
    emergency_contact: Optional[ClientEmergencyContact] = None
    medical_history: Optional[ClientMedicalHistory] = None
    # null: not available (the vitals rule: an in-progress visit or a patient grant, and only for a visit booked
    # for oneself, because readings are filed under the booker). []: available, none recorded yet.
    latest_vitals: Optional[List[ClientVital]] = None
