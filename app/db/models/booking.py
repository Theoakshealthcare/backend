import uuid
from datetime import datetime
from typing import Optional
from enum import Enum

from sqlalchemy import (
    Boolean, Column, DateTime, String, Text, ForeignKey, 
    Float, Integer, JSON, Enum as SQLEnum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

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
    
    # Payment
    total_amount = Column(Float, nullable=False)
    service_fee = Column(Float, nullable=False)
    platform_fee = Column(Float, nullable=False)
    paystack_reference = Column(String, nullable=True)
    
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
