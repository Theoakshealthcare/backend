import uuid
from datetime import datetime
from typing import Optional
from enum import Enum

from sqlalchemy import (
    Boolean, Column, DateTime, String, Text, ForeignKey, 
    BigInteger, Float, Integer, JSON, Enum as SQLEnum, CheckConstraint, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import expression

from app.core.money import kobo_to_naira
from app.db.base import Base


class BookingStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
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
    
    # Location
    patient_address = Column(Text, nullable=False)
    patient_latitude = Column(Float, nullable=False)
    patient_longitude = Column(Float, nullable=False)
    patient_city = Column(String, nullable=False)
    patient_state = Column(String, nullable=False)
    
    # Status
    status = Column(SQLEnum(BookingStatus), default=BookingStatus.PENDING)
    payment_status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.PENDING)
    
    # Unpaid expiry: the expire_unpaid_bookings job cancels a pending, unpaid booking past this time.
    payment_expires_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String, nullable=True)  # e.g. 'unpaid_timeout'
    # Money received for a booking that stays cancelled. The admin refund queue consumes this; nothing
    # refunds automatically.
    refund_required = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    refund_required_reason = Column(Text, nullable=True)  # paid_after_expiry | paid_after_cancellation

    # Dispatch to caregivers, separate from `status` so client enums don't change.
    # NULL = not dispatched (unpaid, or paid before dispatch waves existed).
    dispatch_state = Column(String, nullable=True)  # dispatching | matched | unmatched
    dispatch_started_at = Column(DateTime, nullable=True)

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
