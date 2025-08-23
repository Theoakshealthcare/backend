from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, validator
import uuid

from app.db.models.booking import BookingStatus, PaymentStatus


# Booking Creation
class BookingCreate(BaseModel):
    service_type: Literal["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"]
    assistance_type: str
    condition: Optional[str] = None
    duration_hours: int
    scheduled_date: datetime
    patient_address: str
    patient_latitude: float
    patient_longitude: float
    patient_city: str
    patient_state: str
    special_instructions: Optional[str] = None
    booking_for: str

    @validator('duration_hours')
    def validate_duration(cls, v):
        if v < 1 or v > 24:
            raise ValueError('Duration must be between 1 and 24 hours')
        return v


# Caregiver details for booking response
class CaregiverDetails(BaseModel):
    caregiver_id: uuid.UUID
    full_name: str
    specialization: Optional[str] = None

    class Config:
        from_attributes = True


# Booking Response
class BookingResponse(BaseModel):
    id: uuid.UUID
    booking_code: str
    patient_id: uuid.UUID
    caregiver_id: Optional[uuid.UUID] = None
    caregiver_details: Optional[CaregiverDetails] = None
    service_type: str
    assistance_type: str
    condition: Optional[str] = None
    duration_hours: int
    scheduled_date: datetime
    scheduled_end_date: datetime
    patient_address: str
    patient_latitude: float
    patient_longitude: float
    patient_city: str
    patient_state: str
    status: BookingStatus
    payment_status: PaymentStatus
    total_amount: float
    service_fee: float
    platform_fee: float
    special_instructions: Optional[str] = None
    booking_for: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Caregiver Location
class CaregiverLocationUpdate(BaseModel):
    latitude: float
    longitude: float
    accuracy: Optional[float] = None
    heading: Optional[float] = None
    speed: Optional[float] = None
    is_available: bool = True


class CaregiverLocationResponse(BaseModel):
    id: uuid.UUID
    caregiver_id: uuid.UUID
    latitude: float
    longitude: float
    accuracy: Optional[float] = None
    is_active: bool
    is_available: bool
    recorded_at: datetime

    class Config:
        from_attributes = True


# Booking Actions
class BookingActionRequest(BaseModel):
    action: Literal["accept", "decline", "start", "complete", "cancel"]
    reason: Optional[str] = None


# Chat Message
class ChatMessageCreate(BaseModel):
    message: str
    message_type: str = "text"
    attachment_url: Optional[str] = None


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    sender_id: uuid.UUID
    message: str
    message_type: str
    attachment_url: Optional[str] = None
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


# Booking Note
class BookingNoteCreate(BaseModel):
    title: str
    content: Optional[str] = None
    audio_transcript: Optional[str] = None
    audio_file_url: Optional[str] = None


class BookingNoteResponse(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    caregiver_id: uuid.UUID
    title: str
    content: Optional[str] = None
    audio_transcript: Optional[str] = None
    ai_summary: Optional[str] = None
    ai_keywords: Optional[List[str]] = None
    ai_recommendations: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# Notification
class BookingNotificationResponse(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    title: str
    message: str
    notification_type: str
    is_read: bool
    created_at: datetime
    booking: BookingResponse

    class Config:
        from_attributes = True


# Nearby Caregivers
class NearbyCaregiverResponse(BaseModel):
    caregiver_id: uuid.UUID
    full_name: str
    specialization: Optional[str] = None
    years_of_experience: Optional[int] = None
    distance_km: float
    latitude: float
    longitude: float
    is_available: bool
