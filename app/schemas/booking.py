from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Literal, Union, Dict
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


# Booking Preview (for pricing estimates before booking creation)
class BookingPreviewRequest(BaseModel):
    service_type: Literal["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"] = Field(
        description="Type of healthcare service requested",
        example="caregiver"
    )
    duration_hours: int = Field(
        description="Duration of service in hours (1-24)",
        ge=1, le=24,
        example=4
    )
    patient_latitude: Optional[float] = Field(
        None,
        description="Patient latitude for surge pricing calculation (-90 to 90)",
        ge=-90, le=90,
        example=6.5244
    )
    patient_longitude: Optional[float] = Field(
        None,
        description="Patient longitude for surge pricing calculation (-180 to 180)",
        ge=-180, le=180,
        example=3.3792
    )

    @validator('duration_hours')
    def validate_duration(cls, v):
        if v < 1 or v > 24:
            raise ValueError('Duration must be between 1 and 24 hours')
        return v


class BookingPreviewResponse(BaseModel):
    service_type: str = Field(description="Type of healthcare service", example="caregiver")
    duration_hours: int = Field(description="Service duration in hours", example=4)
    base_fee_per_hour: float = Field(description="Base fee per hour", example=5000.0)
    service_multiplier: float = Field(description="Service type multiplier", example=1.0)
    service_fee: float = Field(description="Total service fee before additional charges", example=20000.0)
    platform_fee: float = Field(description="Platform commission fee", example=2000.0)
    platform_fee_percent: float = Field(description="Platform fee percentage", example=10.0)
    total_amount: float = Field(description="Total amount to be charged", example=22000.0)
    currency: str = Field(description="Currency code", example="NGN")
    
    # Pricing factors
    surge_pricing_active: bool = Field(description="Whether surge pricing is applied", example=False)
    surge_multiplier: Optional[float] = Field(None, description="Surge pricing multiplier", example=1.2)
    discount_active: bool = Field(description="Whether discount is applied", example=False)
    discount_percent: Optional[float] = Field(None, description="Discount percentage", example=15.0)
    
    # Breakdown for transparency
    pricing_breakdown: Dict[str, float] = Field(
        description="Detailed pricing breakdown",
        example={
            "base_calculation": 20000.0,
            "surge_adjustment": 0.0,
            "discount_savings": 0.0,
            "service_fee_final": 20000.0,
            "platform_fee": 2000.0,
            "total": 22000.0
        }
    )
    
    # Additional info
    estimated_caregivers_available: Optional[int] = Field(
        None,
        description="Estimated number of available caregivers in the area",
        example=8
    )
    peak_hours_warning: Optional[str] = Field(
        None,
        description="Warning about peak hours if applicable",
        example="Peak hours: Higher demand may affect caregiver availability"
    )

    class Config:
        from_attributes = True


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
    dispatch_state: Optional[str] = Field(
        None, description="dispatching | matched | unmatched; null before payment or for legacy bookings")
    visit_code: Optional[str] = Field(
        None,
        description="4-digit code the patient gives the caregiver to start the visit. Only returned to the "
                    "booking's own patient from GET /booking/booking/{id}, while the booking is accepted or "
                    "en_route; null for everyone else and in every other response.",
        example="4821",
    )
    # Payment fields for booking creation response
    payment_url: Optional[str] = Field(
        None,
        description="Paystack payment URL to complete the booking payment",
        example="https://checkout.paystack.com/abcd1234"
    )
    payment_access_code: Optional[str] = Field(
        None,
        description="Paystack access code for the payment",
        example="abc123xyz"
    )
    payment_error: Optional[str] = Field(
        None,
        description="Error message if payment initialization failed",
        example="Payment gateway temporarily unavailable"
    )

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
    is_available: Optional[bool] = Field(
        None,
        description="Set availability for new bookings. Omit to leave it unchanged (a first ping "
                    "without it creates the record as unavailable). Prefer POST /caregiver/availability.",
        example=None,
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
class CaregiverActionBooking(BaseModel):
    """The booking as a caregiver sees it in the action response (accept, decline, start, ...).

    Describes what the endpoint has always returned; fields that are null in the offer view
    (patient_details, caregiver_details, caregiver_id, condition, special_instructions) are Optional.
    Money is naira as Decimal (serialised as a string such as "55000.00") next to the *_kobo integers.
    """

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
    actual_start_time: Optional[datetime] = None
    actual_end_time: Optional[datetime] = None
    patient_address: str
    patient_latitude: Optional[float] = None
    patient_longitude: Optional[float] = None
    patient_city: str
    patient_state: str
    status: BookingStatus
    payment_status: PaymentStatus
    paystack_reference: Optional[str] = None
    payment_expires_at: Optional[datetime] = None
    payment_failure_reason: Optional[str] = None
    total_amount: Decimal
    service_fee: Decimal
    platform_fee: Decimal
    total_amount_kobo: int
    service_fee_kobo: int
    platform_fee_kobo: int
    special_instructions: Optional[str] = None
    booking_for: str
    cancel_reason: Optional[str] = None
    refund_required: bool
    refund_required_reason: Optional[str] = None
    dispatch_state: Optional[str] = None
    dispatch_started_at: Optional[datetime] = None
    visit_code: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CaregiverActionResponse(BaseModel):
    message: str
    booking: CaregiverActionBooking


class BookingActionRequest(BaseModel):
    action: Literal["accept", "decline", "on_my_way", "start", "complete", "cancel"]
    reason: Optional[str] = None
    # Required by `start`: the 4-digit code the patient reads out at the door.
    visit_code: Optional[str] = Field(None, pattern=r"^[0-9]{4}$")


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
