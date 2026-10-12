import uuid
from datetime import datetime
from typing import Optional
from enum import Enum

from sqlalchemy import (
    Boolean, Column, DateTime, String, Text, ForeignKey, 
    BigInteger, Float, Integer, JSON, Enum as SQLEnum, CheckConstraint, UniqueConstraint, Index, text
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import expression

from app.core.money import kobo_to_naira
from app.db.base import Base


class BookingStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EN_ROUTE = "en_route"  # accepted -> en_route -> in_progress; the caregiver pressed "on my way"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DECLINED = "declined"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(
            "dispatch_state IN ('dispatching','matched','unmatched')", name="ck_bookings_dispatch_state"
        ),
        CheckConstraint("channel IN ('app','whatsapp','admin')", name="ck_bookings_channel"),
        CheckConstraint("cancel_fee_tier IN ('none','standard','late')", name="ck_bookings_cancel_fee_tier"),
        CheckConstraint(
            "dropout_state IN ('redispatching','awaiting_choice','waiting','handed_over','closed')",
            name="ck_bookings_dropout_state"),
        Index("ix_bookings_dropout_open", "scheduled_date",
              postgresql_where=text("dropout_state IN ('redispatching','awaiting_choice','waiting')")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_code = Column(String, unique=True, nullable=False)
    
    # Relationships
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=True)
    
    # Booking Details
    service_type = Column(String, nullable=False)  # 'nurse', 'doctor', 'physiotherapist', 'caregiver'
    assistance_type = Column(String, nullable=False)
    condition = Column(String, nullable=True)
    duration_hours = Column(Integer, nullable=False)
    
    # Scheduling
    scheduled_date = Column(DateTime, nullable=False)
    scheduled_end_date = Column(DateTime, nullable=False)
    actual_start_time = Column(DateTime, nullable=True)
    actual_end_time = Column(DateTime, nullable=True)
    
    # Where the booking was made, who made it, and who the visit is for (WhatsApp channel, PRD section 10).
    # For app bookings booked_by_user_id and care_recipient_id stay NULL: the patient is the booker.
    channel = Column(String, nullable=False, default="app", server_default="app")
    booked_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    care_recipient_id = Column(UUID(as_uuid=True), ForeignKey("care_recipients.id"), nullable=True, index=True)
    # The condition typed in the WhatsApp booking Flow, Fernet-encrypted (WA-NFR-7). Never echoed in chat.
    # ``condition`` (plaintext, app bookings) stays NULL for these bookings.
    condition_encrypted = Column(Text, nullable=True)

    # Location
    patient_address = Column(Text, nullable=True)  # NULL once an erased patient's booking is finished
    patient_latitude = Column(Float, nullable=True)
    patient_longitude = Column(Float, nullable=True)
    patient_city = Column(String, nullable=False)
    patient_state = Column(String, nullable=False)
    # No map pin was shared, so the coordinates are a city-centre placeholder. Dispatch is held until an
    # operator confirms the address and clears this (WA-FR-14).
    needs_address_check = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    
    # Status
    status = Column(SQLEnum(BookingStatus), default=BookingStatus.PENDING)
    payment_status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.PENDING)
    
    # Unpaid expiry: the expire_unpaid_bookings job cancels a pending, unpaid booking past this time.
    payment_expires_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String, nullable=True)  # e.g. 'unpaid_timeout'
    # Money received for a booking that stays cancelled. The admin refund queue consumes this; nothing
    # refunds automatically.
    refund_required = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    refund_required_reason = Column(Text, nullable=True)  # paid_after_expiry | paid_after_cancellation | cancelled_by_booker | caregiver_cancelled ...
    # What is to be refunded, in kobo, when the policy keeps a cancellation fee (WA-FR-39). NULL = the refund module
    # decides (the full amount for the older reasons above).
    refund_amount_kobo = Column(BigInteger, nullable=True)

    # An admin flagged the assigned caregiver as a no-show (AD-FR-26). Cleared when the booking is reassigned.
    no_show_flagged_at = Column(DateTime, nullable=True)
    no_show_flagged_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)

    # --- the cancellation policy that applies to this booking (set when the quote is made) and what was applied ---
    # ``{"free_hours", "late_hours", "fee_percent", "late_fee_percent"}``, copied from the admin policy when the quote
    # is created, so a later edit never changes terms the family agreed to. NULL (app and admin bookings): the live
    # policy applies.
    cancel_policy = Column(JSON, nullable=True)
    # What the caregiver earns, copied from the admin policy when the booking is created (like ``cancel_policy``):
    # ``{"visit_share_percent", "hold_hours", "late_fee_share_percent"}``. NULL: the live policy applies.
    earnings_terms = Column(JSON, nullable=True)
    cancel_fee_tier = Column(String, nullable=True)  # none | standard | late: the tier a booker cancellation applied
    # For the late tier only: was it because the caregiver set out in good time and really moved toward the patient?
    # (The caregiver-compensation work, A3, reads this.) NULL when the late tier did not apply.
    late_cancel_compensable = Column(Boolean, nullable=True)
    on_my_way_at = Column(DateTime, nullable=True)
    on_my_way_distance_m = Column(Integer, nullable=True)  # caregiver to patient when they tapped, if a fix existed

    # After a caregiver drops out of a WhatsApp booking: redispatching | awaiting_choice | waiting | handed_over |
    # closed. NULL = no drop-out in progress. See app/services/dropout.py.
    dropout_state = Column(String, nullable=True)

    # Dispatch to caregivers, separate from `status` so client enums don't change.
    # NULL = not dispatched (unpaid, or paid before dispatch waves existed).
    dispatch_state = Column(String, nullable=True)  # dispatching | matched | unmatched
    dispatch_started_at = Column(DateTime, nullable=True)

    # Visit code (WA-FR-35): 4 digits generated when the caregiver accepts. Encrypted (not hashed)
    # because the booking's patient must be able to read it back; `start` compares it in constant time.
    # Three wrong guesses lock it for VISIT_CODE_LOCK_MINUTES.
    visit_code_enc = Column(String, nullable=True)
    visit_code_failed_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    visit_code_locked_until = Column(DateTime, nullable=True)

    # Payment
    # Money is integer kobo (BIGINT). The naira properties below are read-only, for responses.
    total_amount_kobo = Column(BigInteger, nullable=False)
    service_fee_kobo = Column(BigInteger, nullable=False)
    platform_fee_kobo = Column(BigInteger, nullable=False)
    paystack_reference = Column(String, nullable=True)
    payment_failure_reason = Column(Text, nullable=True)  # Store payment failure details
    
    # Special Instructions
    special_instructions = Column(Text, nullable=True)
    
    # Metadata
    booking_for = Column(String, nullable=False)  # 'myself', 'mother', 'father', etc.
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    patient = relationship("Patient", back_populates="bookings")
    caregiver = relationship("Caregiver", back_populates="bookings")
    notifications = relationship("BookingNotification", back_populates="booking")
    messages = relationship("ChatMessage", back_populates="booking")
    notes = relationship("BookingNote", back_populates="booking")

    @property
    def total_amount(self):
        return kobo_to_naira(self.total_amount_kobo)

    @property
    def service_fee(self):
        return kobo_to_naira(self.service_fee_kobo)

    @property
    def platform_fee(self):
        return kobo_to_naira(self.platform_fee_kobo)


class BookingDispatchWave(Base):
    """One batch of caregivers notified about a booking. (booking_id, wave_no) is unique, so two
    workers can never both send the same wave."""
    __tablename__ = "booking_dispatch_waves"
    __table_args__ = (UniqueConstraint("booking_id", "wave_no", name="uq_booking_dispatch_waves_booking_wave"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    wave_no = Column(Integer, nullable=False)
    notified_caregiver_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    sent_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class BookingNotification(Base):
    __tablename__ = "booking_notifications"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    
    # Notification Details
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    notification_type = Column(String, nullable=False)  # 'new_booking', 'booking_update', etc.
    
    # Status
    is_read = Column(Boolean, default=False)
    is_sent = Column(Boolean, default=False)
    sent_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)
    
    # Response
    response = Column(String, nullable=True)  # 'accepted', 'declined'
    response_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    booking = relationship("Booking", back_populates="notifications")
    caregiver = relationship("Caregiver", back_populates="notifications")


class CaregiverLocation(Base):
    __tablename__ = "caregiver_locations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    
    # Location Data
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    accuracy = Column(Float, nullable=True)
    heading = Column(Float, nullable=True)
    speed = Column(Float, nullable=True)
    
    # Status
    is_active = Column(Boolean, default=True)
    is_available = Column(Boolean, default=True)
    
    # Timestamps
    recorded_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    caregiver = relationship("Caregiver", back_populates="locations")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    # Message Content
    message = Column(Text, nullable=False)
    message_type = Column(String, default="text")  # 'text', 'image', 'audio', 'location'
    attachment_url = Column(String, nullable=True)
    
    # Status
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    booking = relationship("Booking", back_populates="messages")
    sender = relationship("User")


class BookingNote(Base):
    __tablename__ = "booking_notes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    
    # Note Content
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    audio_transcript = Column(Text, nullable=True)
    audio_file_url = Column(String, nullable=True)
    
    # AI Processing
    ai_summary = Column(Text, nullable=True)
    ai_keywords = Column(JSON, nullable=True)
    ai_recommendations = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    booking = relationship("Booking", back_populates="notes")
    caregiver = relationship("Caregiver", back_populates="notes")
