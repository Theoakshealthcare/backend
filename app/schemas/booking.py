from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Literal, Union, Dict
from pydantic import BaseModel, validator, Field, field_validator, model_validator
import uuid

from app.db.models.booking import BookingStatus, PaymentStatus


# A length in hours: ``4`` stays an int, ``1.5`` is a float (a JSON number either way).
Hours = Union[int, float]

_DURATION_DESCRIPTION = (
    "Length of the visit in minutes (30-minute steps by default; the service's own minimum, step and maximum are in "
    "GET /booking/services). Send this OR duration_hours.")


def resolve_duration(minutes: Optional[int], hours: Optional[float]) -> int:
    """The booked length in whole minutes from what a client sent: ``duration_minutes``, the older ``duration_hours``
    (converted: 4 -> 240, 1.5 -> 90), or both when they agree. Whether the length is allowed for the service is the
    catalogue's call (``422 invalid_duration``), not this function's."""
    from_hours = None
    if hours is not None:
        try:
            exact = Decimal(str(hours)) * 60
        except InvalidOperation:
            raise ValueError("duration_hours must be a number")
        if exact != exact.to_integral_value():
            raise ValueError("duration_hours must be a whole number of minutes (for example 1.5 for 90 minutes)")
        from_hours = int(exact)
    if minutes is None and from_hours is None:
        raise ValueError("duration_minutes (or duration_hours) is required")
    if minutes is not None and from_hours is not None and minutes != from_hours:
        raise ValueError("duration_minutes and duration_hours disagree; send one of them")
    return minutes if minutes is not None else from_hours


def _refuse_boolean(value):
    """JSON ``true`` / ``false`` must not be read as 1 / 0 minutes or hours (pydantic's lax mode would accept them)."""
    if isinstance(value, bool):
        raise ValueError("must be a number, not true or false")
    return value


def _hours(minutes: int) -> Hours:
    return minutes // 60 if minutes % 60 == 0 else minutes / 60


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
    duration_minutes: Optional[int] = Field(None, description=_DURATION_DESCRIPTION, example=90)
    duration_hours: Optional[Hours] = Field(
        None, allow_inf_nan=False,
        description="Older way to send the length, in hours (4 or 1.5). Converted to minutes; both are returned.",
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

    _no_booleans = field_validator("duration_minutes", "duration_hours", mode="before")(_refuse_boolean)

    @model_validator(mode="after")
    def _resolve_duration(self):
        self.duration_minutes = resolve_duration(self.duration_minutes, self.duration_hours)
        self.duration_hours = _hours(self.duration_minutes)
        return self


# Booking Preview (for pricing estimates before booking creation)
class BookingPreviewRequest(BaseModel):
    service_type: Literal["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"] = Field(
        description="Type of healthcare service requested",
        example="caregiver"
    )
    duration_minutes: Optional[int] = Field(None, description=_DURATION_DESCRIPTION, example=90)
    duration_hours: Optional[Hours] = Field(
        None, allow_inf_nan=False,
        description="Older way to send the length, in hours (4 or 1.5). Converted to minutes; both are returned.",
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

    _no_booleans = field_validator("duration_minutes", "duration_hours", mode="before")(_refuse_boolean)

    @model_validator(mode="after")
    def _resolve_duration(self):
        self.duration_minutes = resolve_duration(self.duration_minutes, self.duration_hours)
        self.duration_hours = _hours(self.duration_minutes)
        return self


class BookingPreviewResponse(BaseModel):
    service_type: str = Field(description="Type of healthcare service", example="caregiver")
    duration_minutes: int = Field(description="Service duration in minutes", example=240)
    duration_hours: Hours = Field(description="Service duration in hours (1.5 for 90 minutes)", example=4)
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


# Refund summary on a booking (patient view): what the family is shown, not the internal refund states
class RefundSummary(BaseModel):
    status: Literal["under_review", "processing", "needs_bank_details", "refunded", "declined", "contact_support"] = Field(
        description="under_review: waiting for a person to approve it. processing: approved and on its way (also while Finance "
                    "retries a failed attempt). needs_bank_details: the bank asked for the family's account; support will "
                    "ask for it. refunded: money sent. declined: not refunded. contact_support: closed without a payout; "
                    "support follows up.")
    amount: Optional[float] = Field(
        None, description="Naira that will be (or was) refunded; null while a person still has to decide the amount",
        example=36000.0)

    class Config:
        from_attributes = True


# Booking Response
class BookingResponse(BaseModel):
    id: uuid.UUID
    booking_code: str
    channel: Literal["app", "whatsapp", "admin"]
    patient_id: uuid.UUID
    patient_details: Optional[PatientDetails] = None
    caregiver_id: Optional[uuid.UUID] = None
    caregiver_details: Optional[CaregiverDetails] = None
    service_type: str
    assistance_type: str
    condition: Optional[str] = None
    duration_minutes: int
    duration_hours: Hours
    scheduled_date: datetime
    scheduled_end_date: datetime
    patient_address: Optional[str] = None
    patient_latitude: Optional[float] = None
    patient_longitude: Optional[float] = None
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
    arrived_at: Optional[datetime] = Field(
        None, description="When the assigned caregiver tapped \"I've arrived\" (UTC); null until then. The status does not "
                          "change: the visit still starts with the visit code.")
    caregiver_earning: Optional[float] = Field(
        None, description="Naira the assigned caregiver earns for completing this visit (their share of the service fee, from "
                          "the terms copied onto the booking when it was created). Caregiver views only, for an accepted, "
                          "en route, in progress or completed booking; null for everyone else and when the booking carries "
                          "no snapshot.", example=12000.0)
    visit_code: Optional[str] = Field(
        None,
        description="4-digit code the patient gives the caregiver to start the visit. Only returned to the "
                    "booking's own patient from GET /booking/booking/{id}, while the booking is accepted or "
                    "en_route; null for everyone else and in every other response.",
        example="4821",
    )
    cancel_fee_tier: Optional[Literal["none", "standard", "late"]] = Field(
        None, description="Which cancellation fee applied when the booker cancelled (none = free). Null while the booking is "
                          "not cancelled by its booker, and always null for caregivers.")
    cancel_fee: Optional[float] = Field(
        None, description="Naira the booker was charged for cancelling (0 for a free cancel). Null unless the booker "
                          "cancelled; patient views only.", example=8000.0)
    refund: Optional[RefundSummary] = Field(
        None, description="The booking's refund, newest one, when it has one. Patient views only; null for caregivers.")
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


class PaymentLinkResponse(BookingResponse):
    """``POST /booking/{id}/payment-link``: what ``/create`` returns, plus the reference and when the window closes."""
    paystack_reference: str = Field(description="The booking's one Paystack reference (the same for every link)",
                                    example="BKG_AB12CD34_0a1b2c3d")
    payment_expires_at: Optional[datetime] = Field(
        None, description="When the unpaid booking is cancelled if it is still not paid (UTC)")


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
    channel: Literal["app", "whatsapp", "admin"]
    patient_id: uuid.UUID
    patient_details: Optional[PatientDetails] = None
    caregiver_id: Optional[uuid.UUID] = None
    caregiver_details: Optional[CaregiverDetails] = None
    service_type: str
    assistance_type: str
    condition: Optional[str] = None
    duration_minutes: int
    duration_hours: Hours
    scheduled_date: datetime
    scheduled_end_date: datetime
    actual_start_time: Optional[datetime] = None
    actual_end_time: Optional[datetime] = None
    patient_address: Optional[str] = None
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
    arrived_at: Optional[datetime] = None
    caregiver_earning: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class CaregiverActionResponse(BaseModel):
    message: str
    booking: CaregiverActionBooking


class BookingActionRequest(BaseModel):
    action: Literal["accept", "decline", "on_my_way", "arrived", "start", "complete", "cancel"]
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
    # The caregiver's answer to this offer: null while unanswered, "accept", "declined" (final: Accept is refused), or a
    # system value ("auto_declined", "booking_cancelled", "reassigned", "assigned_by_admin"). The app hides Accept/Decline
    # unless it is null.
    response: Optional[str] = None
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


# Cancelling from the patient app (P4-BE)
class CancelPolicyOut(BaseModel):
    free_hours: int = Field(description="Cancelling more than this many hours before the visit is free", example=12)
    late_hours: int = Field(description="Cancelling less than this many hours before the visit (or once the caregiver is on "
                                        "the way) pays the late fee", example=2)
    fee_percent: int = Field(description="Percent of the service fee kept between late_hours and free_hours", example=20)
    late_fee_percent: int = Field(description="Percent of the service fee kept inside late_hours or en route", example=50)


class CancelTermsResponse(BaseModel):
    booking_id: uuid.UUID
    booking_code: str
    can_cancel: bool
    reason: Optional[Literal["booking_in_progress", "booking_completed", "booking_already_cancelled", "booking_closed"]] = Field(
        None, description="Why it cannot be cancelled (null when it can); the same codes the cancel endpoint's 409 uses")
    message: Optional[str] = Field(None, description="Plain-language version of reason, safe to show")
    fee_tier: Literal["none", "standard", "late"]
    fee_percent: int = Field(description="Percent of the SERVICE fee kept (0 for the free tier)")
    fee_amount: float = Field(description="Naira kept if the booking is cancelled now", example=8000.0)
    fee_amount_kobo: int = Field(description="Same in kobo: send it back as accepted_fee_kobo when confirming", example=80000)
    paid_amount: float = Field(description="Naira paid so far (0 when the booking is not paid yet)")
    paid_amount_kobo: int
    refundable_amount: float = Field(description="Naira refunded if cancelled now: paid minus fee_amount")
    refundable_amount_kobo: int
    refund_will_be_queued: bool = Field(description="True when cancelling queues a refund (a paid booking). Never automatic: "
                                                    "a person approves it.")
    refund_destination: Optional[Literal["original_method"]] = Field(
        None, description="Where the refund goes: the original payment method (Paystack). Null when no refund is queued.")
    refund_needs_approval: bool
    caregiver_assigned: bool
    policy: Optional[CancelPolicyOut] = None
    as_of: datetime = Field(description="When these terms were worked out (UTC). They can change: confirm with accepted_fee_kobo.")


class CancelBookingRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500, description="Why the family is cancelling. Optional; shown to the "
                                                                    "person who approves the refund.")
    accepted_fee_kobo: Optional[int] = Field(
        None, ge=0, description="The fee the person saw and agreed to (fee_amount_kobo from cancel-terms). If the fee is "
                                "higher when the cancel is decided, nothing is cancelled and the response is 409 "
                                "terms_changed. Omit only if you do not show a fee.")


class CancelBookingResponse(BaseModel):
    booking_id: uuid.UUID
    booking_code: str
    status: Literal["cancelled"]
    already_cancelled: bool = Field(description="True when this is a repeat of a cancel the booker already made: nothing "
                                                "was changed and the first result is returned again")
    fee_tier: Literal["none", "standard", "late"]
    fee_percent: int
    fee_amount: float
    fee_amount_kobo: int
    paid_amount: float
    paid_amount_kobo: int
    refundable_amount: float
    refundable_amount_kobo: int
    refund_queued: bool
    refund_destination: Optional[Literal["original_method"]] = None
    refund_needs_approval: bool
    caregiver_assigned: bool
    refund: Optional[RefundSummary] = None
