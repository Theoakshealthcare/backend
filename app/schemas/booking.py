from datetime import datetime
from typing import Optional, List, Literal, Union
from pydantic import BaseModel, validator, Field
import uuid

from app.db.models.booking import BookingStatus, PaymentStatus


# Booking Creation
class BookingCreate(BaseModel):
    service_type: Literal["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"] = Field(
        description="Type of healthcare service requested",
        example="caregiver"
    )
    assistance_type: str = Field(
        description="Specific type of assistance needed",
        example="Personal care and medication assistance"
    )
    condition: Optional[str] = Field(
        None,
        description="Medical condition or specific health needs",
        example="Diabetes management"
    )
    duration_hours: int = Field(
        description="Duration of service in hours (1-24)",
        ge=1, le=24,
        example=4
    )
    scheduled_date: datetime = Field(
        description="When the service should be provided (ISO 8601 format)",
        example="2024-01-15T14:00:00Z"
    )
    patient_address: str = Field(
        description="Full address where service will be provided",
        example="123 Main Street, Lagos, Nigeria"
    )
    patient_latitude: float = Field(
        description="Latitude coordinate of service location (-90 to 90)",
        ge=-90, le=90,
        example=6.5244
    )
    patient_longitude: float = Field(
        description="Longitude coordinate of service location (-180 to 180)",
        ge=-180, le=180,
        example=3.3792
    )
    patient_city: str = Field(
        description="City where service will be provided",
        example="Lagos"
    )
    patient_state: str = Field(
        description="State where service will be provided",
        example="Lagos State"
    )
    special_instructions: Optional[str] = Field(
        None,
        description="Additional instructions for the caregiver",
        example="Patient prefers female caregiver. Needs help with insulin injection at 3 PM."
    )
    booking_for: str = Field(
        description="Who the booking is for",
        example="Myself"
    )

    @validator('duration_hours')
    def validate_duration(cls, v):
        if v < 1 or v > 24:
            raise ValueError('Duration must be between 1 and 24 hours')
        return v


# Caregiver details for booking response
class CaregiverDetails(BaseModel):
    caregiver_id: uuid.UUID = Field(description="Unique caregiver identifier")
    full_name: str = Field(description="Caregiver's full name", example="Dr. John Smith")
    specialization: Optional[str] = Field(None, description="Caregiver's specialization", example="Registered Nurse")
    profile_image_url: Optional[str] = Field(
        None, 
        description="Caregiver's profile image URL", 
        example="https://cdn.example.com/profiles/caregiver123.jpg"
    )
    # Location data
    current_latitude: Optional[float] = Field(
        None, 
        description="Current GPS latitude of caregiver (-90 to 90)", 
        example=6.5244,
        ge=-90, le=90
    )
    current_longitude: Optional[float] = Field(
        None, 
        description="Current GPS longitude of caregiver (-180 to 180)", 
        example=3.3792,
        ge=-180, le=180
    )
    distance_from_patient: Optional[float] = Field(
        None, 
        description="Distance from caregiver to patient in kilometers", 
        example=2.5,
        ge=0
    )
    estimated_arrival_time: Optional[int] = Field(
        None, 
        description="Estimated arrival time in minutes", 
        example=15,
        ge=0
    )
    last_location_update: Optional[datetime] = Field(
        None, 
        description="Timestamp of last GPS location update", 
        example="2024-01-15T10:30:00Z"
    )
    is_online: Optional[bool] = Field(
        None, 
        description="Whether caregiver is currently online and trackable", 
        example=True
    )

    class Config:
        from_attributes = True


# Patient details for booking response
class PatientDetails(BaseModel):
    patient_id: uuid.UUID = Field(description="Unique patient identifier")
    full_name: str = Field(description="Patient's full name", example="Jane Doe")
    phone_number: Optional[str] = Field(None, description="Patient's phone number", example="+234123456789")
    profile_image_url: Optional[str] = Field(
        None, 
        description="Patient's profile image URL", 
        example="https://cdn.example.com/profiles/patient123.jpg"
    )
    emergency_contact_name: Optional[str] = Field(None, description="Emergency contact name", example="John Doe")
    emergency_contact_phone: Optional[str] = Field(None, description="Emergency contact phone", example="+234987654321")

    class Config:
        from_attributes = True


# Booking Response
class BookingResponse(BaseModel):
    id: uuid.UUID
    booking_code: str
    patient_id: uuid.UUID
    patient_details: Optional[PatientDetails] = None
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
    latitude: float = Field(
        description="GPS latitude coordinate (-90 to 90)",
        ge=-90, le=90,
        example=6.5244
    )
    longitude: float = Field(
        description="GPS longitude coordinate (-180 to 180)",
        ge=-180, le=180,
        example=3.3792
    )
    accuracy: Optional[float] = Field(
        None,
        description="GPS accuracy in meters (lower is better)",
        example=10.5,
        ge=0
    )
    heading: Optional[float] = Field(
        None,
        description="Direction of movement in degrees (0-360, where 0 is North)",
        example=45.0,
        ge=0, le=360
    )
    speed: Optional[float] = Field(
        None,
        description="Movement speed in meters per second",
        example=2.5,
        ge=0
    )
    is_available: bool = Field(
        True,
        description="Whether caregiver is available for new bookings",
        example=True
    )


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


# Live Tracking Response
class LiveTrackingResponse(BaseModel):
    booking_id: uuid.UUID = Field(description="Unique booking identifier")
    caregiver_id: uuid.UUID = Field(description="Unique caregiver identifier")
    caregiver_name: str = Field(description="Caregiver's full name", example="Dr. John Smith")
    current_latitude: float = Field(
        description="Current GPS latitude of caregiver", 
        example=6.5244,
        ge=-90, le=90
    )
    current_longitude: float = Field(
        description="Current GPS longitude of caregiver", 
        example=3.3792,
        ge=-180, le=180
    )
    distance_km: float = Field(
        description="Current distance from caregiver to patient in kilometers", 
        example=2.5,
        ge=0
    )
    estimated_arrival_minutes: int = Field(
        description="Estimated arrival time in minutes based on current location", 
        example=15,
        ge=0
    )
    last_updated: datetime = Field(
        description="Timestamp when location was last updated", 
        example="2024-01-15T10:30:00Z"
    )
    is_moving: bool = Field(
        description="Whether caregiver is currently moving (speed > 0.5 m/s)", 
        example=True
    )
    booking_status: str = Field(
        description="Current status of the booking", 
        example="accepted"
    )

    class Config:
        from_attributes = True


# WebSocket Message Schemas
class WebSocketMessageType:
    LOCATION_UPDATE = "location_update"
    TRACKING_SUBSCRIBE = "tracking_subscribe"
    TRACKING_UNSUBSCRIBE = "tracking_unsubscribe"
    LOCATION_BROADCAST = "location_broadcast"
    CONNECTION_STATUS = "connection_status"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class WebSocketLocationUpdate(BaseModel):
    type: Literal["location_update"] = Field("location_update", description="Message type")
    data: CaregiverLocationUpdate = Field(description="Location update data")


class WebSocketTrackingSubscribe(BaseModel):
    type: Literal["tracking_subscribe"] = Field("tracking_subscribe", description="Message type")
    booking_id: str = Field(description="Booking ID to track")


class WebSocketTrackingUnsubscribe(BaseModel):
    type: Literal["tracking_unsubscribe"] = Field("tracking_unsubscribe", description="Message type") 
    booking_id: str = Field(description="Booking ID to stop tracking")


class WebSocketLocationBroadcast(BaseModel):
    type: Literal["location_broadcast"] = Field("location_broadcast", description="Message type")
    booking_id: str = Field(description="Booking being tracked")
    data: LiveTrackingResponse = Field(description="Live tracking data")


class WebSocketConnectionStatus(BaseModel):
    type: Literal["connection_status"] = Field("connection_status", description="Message type")
    status: Literal["connected", "disconnected", "reconnected"] = Field(description="Connection status")
    message: str = Field(description="Status message")


class WebSocketError(BaseModel):
    type: Literal["error"] = Field("error", description="Message type")
    error: str = Field(description="Error message")
    code: Optional[str] = Field(None, description="Error code")


class WebSocketHeartbeat(BaseModel):
    type: Literal["heartbeat"] = Field("heartbeat", description="Message type")
    timestamp: str = Field(description="Heartbeat timestamp")


# Union type for all WebSocket messages
WebSocketMessage = Union[
    WebSocketLocationUpdate,
    WebSocketTrackingSubscribe, 
    WebSocketTrackingUnsubscribe,
    WebSocketLocationBroadcast,
    WebSocketConnectionStatus,
    WebSocketError,
    WebSocketHeartbeat
]
