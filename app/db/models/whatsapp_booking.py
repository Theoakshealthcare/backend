"""Booking in chat (W2): the quote that locks a price, and the booking-event outbox.

``quotes`` doubles as the draft of a booking while the Flow is open: a row is created when the Flow is sent
and moves ``draft -> submitted -> quoted -> confirmed``. Anything sensitive the person typed in the Flow
(the condition, special instructions) lives in ``sensitive_encrypted`` only, Fernet-encrypted, and is wiped as
soon as it has been copied onto the booking or the draft is abandoned. ``payload`` holds logistics and the
price breakdown in kobo, never health text.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base

QUOTE_STATUSES = ("draft", "submitted", "out_of_area", "quoted", "confirmed", "cancelled", "expired", "waitlist")
OPEN_QUOTE_STATUSES = ("draft", "submitted", "out_of_area", "quoted")  # still waiting on the person


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in QUOTE_STATUSES) + ")", name="ck_quotes_status"
        ),
        CheckConstraint("total_kobo IS NULL OR total_kobo > 0", name="ck_quotes_total_positive"),
        Index("ix_quotes_contact_status", "contact_id", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("whatsapp_contacts.id"), nullable=False)
    status = Column(String, nullable=False, default="draft", server_default="draft")
    payload = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    sensitive_encrypted = Column(Text, nullable=True)
    # The booking settings row the price came from, and the price, locked for WHATSAPP_QUOTE_TTL_MINUTES.
    settings_id = Column(UUID(as_uuid=True), ForeignKey("booking_settings.id"), nullable=True)
    total_kobo = Column(BigInteger, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    # One quote becomes at most one booking, whatever the retries: the unique key is the last line of defence.
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True, unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


BOOKING_EVENTS = ("payment_received", "payment_link_expiring", "booking_cancelled_unpaid", "payment_refund_pending")


class BookingEvent(Base):
    """Something that happened to a booking and may need telling to the person (an outbox).

    Domain code records the event in the same transaction as the change (payment settled, booking
    cancelled); a worker delivers it. ``(booking_id, event)`` is unique, so a reminder or a confirmation
    can never be queued twice. W3's NotificationService generalises this to push and email.
    """
    __tablename__ = "booking_events"
    __table_args__ = (
        UniqueConstraint("booking_id", "event", name="uq_booking_events_booking_event"),
        CheckConstraint(
            "event IN (" + ", ".join(f"'{e}'" for e in BOOKING_EVENTS) + ")", name="ck_booking_events_event"
        ),
        Index("ix_booking_events_pending", "delivered_at", "attempts"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    event = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    delivered_at = Column(DateTime, nullable=True)  # set when sent, or when there was nobody to send it to
    # A worker claims an event with a compare-and-set on this column (and counts the attempt), so two workers
    # never send the same one; a claim that dies is retried after a couple of minutes.
    claimed_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(String, nullable=True)  # Meta's error code or a short reason; never a message body
