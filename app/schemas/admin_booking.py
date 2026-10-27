"""Admin bookings operations (AD-FR-19..27): request bodies and response models.

Money is integer kobo throughout. Datetimes are naive UTC, like every other timestamp in the API.
"""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BookingStatusName = Literal["pending", "accepted", "en_route", "in_progress", "completed", "cancelled", "declined"]
PaymentStatusName = Literal["pending", "paid", "failed", "partially_refunded", "refunded"]
DispatchStateName = Literal["dispatching", "matched", "unmatched"]
ChannelName = Literal["app", "whatsapp", "admin"]
ServiceTypeName = Literal["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"]
SortField = Literal["scheduled_date", "created_at", "total_amount_kobo"]
SortOrder = Literal["asc", "desc"]
QueueReason = Literal["address_check", "unmatched", "sla_breached"]

CancelReasonCode = Literal["patient_request", "no_supply", "caregiver_no_show", "safety", "duplicate", "other"]
RefundChoice = Literal["full", "partial", "none"]
CorrectableStatus = Literal["en_route", "in_progress", "completed"]
ActionName = Literal[
    "redispatch", "assign", "force_assign", "reassign", "cancel", "confirm_address", "flag_no_show",
    "correct_status",
]

# A rough box around Nigeria. It exists to catch a swapped or mistyped coordinate, not to be a border.
NG_LAT = (3.5, 14.0)
NG_LNG = (2.5, 15.0)


def _non_blank(value: Optional[str]) -> Optional[str]:
    if value is not None:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- requests ------------------------------------------------------------------------------------


class ReasonIn(_Strict):
    reason: str = Field(max_length=2000)

    _reason = field_validator("reason")(_non_blank)


class RedispatchIn(ReasonIn):
    radius_km: Optional[float] = Field(default=None, ge=1, le=100)
    include_previously_notified: bool = False


class ReassignIn(ReasonIn):
    caregiver_id: UUID


class AssignIn(ReassignIn):
    # Super Admin only: skip the caregiver's own accept and assign straight away (AD-FR-22).
    force: bool = False


class CancelIn(ReasonIn):
    reason_code: CancelReasonCode
    refund: RefundChoice
    refund_amount_kobo: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _amount_matches_choice(self) -> "CancelIn":
        if self.refund == "partial" and self.refund_amount_kobo is None:
            raise ValueError("refund_amount_kobo is required for a partial refund")
        if self.refund != "partial" and self.refund_amount_kobo is not None:
            raise ValueError("refund_amount_kobo is only for a partial refund")
        return self


class ConfirmAddressIn(ReasonIn):
    latitude: float
    longitude: float
    address: Optional[str] = Field(default=None, max_length=1000)

    _address = field_validator("address")(_non_blank)

    @model_validator(mode="after")
    def _inside_nigeria(self) -> "ConfirmAddressIn":
        if not (NG_LAT[0] <= self.latitude <= NG_LAT[1] and NG_LNG[0] <= self.longitude <= NG_LNG[1]):
            raise ValueError("latitude/longitude are outside Nigeria; check they are not swapped")
        return self


class CorrectStatusIn(ReasonIn):
    status: CorrectableStatus


# --- responses -----------------------------------------------------------------------------------


class PartyOut(BaseModel):
    id: UUID
    full_name: str
    phone: Optional[str] = None
    masked: bool  # true when name and phone are shortened because the admin lacks people.pii


class BookingListItem(BaseModel):
    id: UUID
    booking_code: str
    service_type: str
    scheduled_date: datetime
    scheduled_end_date: datetime
    city: str
    state: str
    patient: PartyOut
    caregiver: Optional[PartyOut] = None
    status: BookingStatusName
    payment_status: PaymentStatusName
    dispatch_state: Optional[DispatchStateName] = None
    total_amount_kobo: int
    channel: ChannelName
    created_at: datetime
    age_minutes: int
    needs_address_check: bool
    refund_required: bool
    no_show_flagged_at: Optional[datetime] = None
    no_show_suspected: bool


class BookingList(BaseModel):
    items: List[BookingListItem]
    total: int
    limit: int
    offset: int


class UnmatchedItem(BookingListItem):
    queue_reason: QueueReason
    waiting_minutes: int  # since dispatch began (or since the booking was created, when it never did)


class UnmatchedList(BaseModel):
    items: List[UnmatchedItem]
    total: int
    limit: int
    offset: int


class AddressOut(BaseModel):
    city: str
    state: str
    visible: bool  # false: the admin lacks bookings.address, so only the area is shown
    line: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    needs_address_check: bool


class ClinicalOut(BaseModel):
    """The condition and special instructions. Present only with clinical.read; reading them is audited."""
    masked: bool
    has_condition: bool
    has_special_instructions: bool
    condition: Optional[str] = None
    special_instructions: Optional[str] = None


class PriceOut(BaseModel):
    total_kobo: int
    service_fee_kobo: int
    platform_fee_kobo: int


class PaymentAttemptOut(BaseModel):
    id: UUID
    type: str
    status: str
    amount_kobo: int
    paystack_reference: Optional[str] = None
    created_at: datetime


class PaymentOut(BaseModel):
    status: PaymentStatusName
    paystack_reference: Optional[str] = None
    failure_reason: Optional[str] = None
    expires_at: Optional[datetime] = None
    refund_required: bool
    refund_required_reason: Optional[str] = None
    refund_amount_kobo: Optional[int] = None  # null = the refund module decides (see the booking's reason)
    attempts: List[PaymentAttemptOut]


class WaveOut(BaseModel):
    wave_no: int
    sent_at: datetime
    expires_at: datetime
    notified_count: int


class OfferOut(BaseModel):
    caregiver_id: UUID
    caregiver_name: str
    offered_at: datetime
    response: Optional[str] = None
    response_at: Optional[datetime] = None


class DispatchOut(BaseModel):
    state: Optional[DispatchStateName] = None
    started_at: Optional[datetime] = None
    waves: List[WaveOut]
    offers: List[OfferOut]


class EventOut(BaseModel):
    event: str
    created_at: datetime
    delivered_at: Optional[datetime] = None
    attempts: int
    last_error: Optional[str] = None


TimelineKind = Literal[
    "booking_created", "dispatch_started", "wave_sent", "offer_response", "notification_queued",
    "notification_delivered", "visit_started", "visit_completed", "admin_action",
]


class TimelineEntry(BaseModel):
    at: datetime
    kind: TimelineKind
    title: str
    actor: Optional[str] = None  # "system", "caregiver: <name>" or "admin: <name>"
    detail: Optional[str] = None


class BookingDetail(BaseModel):
    id: UUID
    booking_code: str
    service_type: str
    assistance_type: str
    booking_for: str
    duration_hours: int
    scheduled_date: datetime
    scheduled_end_date: datetime
    actual_start_time: Optional[datetime] = None
    actual_end_time: Optional[datetime] = None
    status: BookingStatusName
    channel: ChannelName
    cancel_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    age_minutes: int
    patient: PartyOut
    caregiver: Optional[PartyOut] = None
    address: AddressOut
    clinical: ClinicalOut
    price: PriceOut
    payment: PaymentOut
    dispatch: DispatchOut
    events: List[EventOut]
    no_show_flagged_at: Optional[datetime] = None
    no_show_suspected: bool
    note_count: int
    timeline: List[TimelineEntry]
    available_actions: List[ActionName]


class CandidateOut(BaseModel):
    caregiver_id: UUID
    full_name: str
    specialization: Optional[str] = None
    distance_km: float
    estimated_arrival_minutes: int
    match_score: float


class CandidateList(BaseModel):
    items: List[CandidateOut]


class BookingActionOut(BaseModel):
    """What changed. Fetch the detail for the rest."""
    id: UUID
    booking_code: str
    status: BookingStatusName
    payment_status: PaymentStatusName
    dispatch_state: Optional[DispatchStateName] = None
    caregiver_id: Optional[UUID] = None
    needs_address_check: bool
    refund_required: bool
    refund_required_reason: Optional[str] = None
    refund_amount_kobo: Optional[int] = None
    no_show_flagged_at: Optional[datetime] = None
    notified_caregivers: int = 0  # redispatch, assign (offer), confirm-address: how many caregivers were offered the visit
