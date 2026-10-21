"""Refunds v2: one row per refund of a booking, and one row per call to Paystack for it.

A refund is created ``pending_approval`` (by a cancel path or by staff), approved by a person, then sent to Paystack by a
worker. ``refund_attempts`` mirrors ``payout_attempts``: a retry adds a row and nothing is overwritten. See
docs/plans/2026-10-21-refunds-v2-design.md section 4.

Statuses:
- OPEN (not finished; at most one per booking, enforced by a partial unique index): pending_approval, approved, processing,
  needs_attention, needs_review, failed. A ``failed`` refund can still be retried, so it keeps the booking's slot until an
  admin closes it.
- COMMITTED (counted against the booking's refundable amount): approved, processing, needs_attention, failed, needs_review,
  succeeded.
- TERMINAL: succeeded, rejected, closed.
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

REFUND_PENDING_APPROVAL, REFUND_APPROVED, REFUND_PROCESSING = "pending_approval", "approved", "processing"
REFUND_NEEDS_ATTENTION, REFUND_NEEDS_REVIEW, REFUND_FAILED = "needs_attention", "needs_review", "failed"
REFUND_SUCCEEDED, REFUND_REJECTED, REFUND_CLOSED = "succeeded", "rejected", "closed"
REFUND_OPEN_STATUSES = (REFUND_PENDING_APPROVAL, REFUND_APPROVED, REFUND_PROCESSING, REFUND_NEEDS_ATTENTION,
                        REFUND_NEEDS_REVIEW, REFUND_FAILED)
REFUND_COMMITTED_STATUSES = (REFUND_APPROVED, REFUND_PROCESSING, REFUND_NEEDS_ATTENTION, REFUND_FAILED,
                             REFUND_NEEDS_REVIEW, REFUND_SUCCEEDED)
REFUND_TERMINAL_STATUSES = (REFUND_SUCCEEDED, REFUND_REJECTED, REFUND_CLOSED)
REFUND_STATUSES = REFUND_OPEN_STATUSES + REFUND_TERMINAL_STATUSES

REFUND_REASON_CODES = ("cancelled_by_booker", "caregiver_cancelled", "no_caregiver_found", "admin_cancelled",
                       "paid_after_expiry", "paid_after_cancellation", "complaint", "other")
REFUND_ORIGIN_SYSTEM, REFUND_ORIGIN_STAFF = "system", "staff"
REFUND_DEST_ORIGINAL, REFUND_DEST_WALLET = "original_method", "wallet"

ATTEMPT_SENDING, ATTEMPT_PENDING, ATTEMPT_PROCESSING = "sending", "pending", "processing"
ATTEMPT_NEEDS_ATTENTION, ATTEMPT_PROCESSED, ATTEMPT_FAILED, ATTEMPT_UNKNOWN = "needs_attention", "processed", "failed", "unknown"
REFUND_ATTEMPT_STATUSES = (ATTEMPT_SENDING, ATTEMPT_PENDING, ATTEMPT_PROCESSING, ATTEMPT_NEEDS_ATTENTION, ATTEMPT_PROCESSED,
                           ATTEMPT_FAILED, ATTEMPT_UNKNOWN)


def _in(column: str, values) -> str:
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount_kobo IS NULL OR amount_kobo > 0", name="ck_refunds_amount_positive"),
        CheckConstraint("suggested_amount_kobo IS NULL OR suggested_amount_kobo >= 0", name="ck_refunds_suggested_not_negative"),
        CheckConstraint("take_back_kobo >= 0", name="ck_refunds_take_back_not_negative"),
        CheckConstraint("take_back_debited_kobo >= 0 AND take_back_owed_kobo >= 0",
                        name="ck_refunds_take_back_split_not_negative"),
        CheckConstraint(_in("status", REFUND_STATUSES), name="ck_refunds_status"),
        CheckConstraint(_in("reason_code", REFUND_REASON_CODES), name="ck_refunds_reason_code"),
        CheckConstraint(_in("origin", (REFUND_ORIGIN_SYSTEM, REFUND_ORIGIN_STAFF)), name="ck_refunds_origin"),
        CheckConstraint("destination IS NULL OR " + _in("destination", (REFUND_DEST_ORIGINAL, REFUND_DEST_WALLET)),
                        name="ck_refunds_destination"),
        # One open refund per booking: also what makes webhook matching by transaction reference unambiguous.
        Index("uq_refunds_one_open_per_booking", "booking_id", unique=True,
              postgresql_where=text(_in("status", REFUND_OPEN_STATUSES))),
        Index("ix_refunds_status", "status", "created_at"),
        Index("ix_refunds_booking", "booking_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)  # who gets the money
    reason_code = Column(String, nullable=False)
    origin = Column(String, nullable=False)
    requested_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)  # null for system
    suggested_amount_kobo = Column(BigInteger, nullable=True)  # what the policy says, frozen at creation
    amount_kobo = Column(BigInteger, nullable=True)  # set at approval (defaults to the suggestion)
    destination = Column(String, nullable=True)  # null until approved
    status = Column(String, nullable=False, default=REFUND_PENDING_APPROVAL)
    approved_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_reason = Column(Text, nullable=True)
    take_back_from_caregiver = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    take_back_kobo = Column(BigInteger, nullable=False, default=0, server_default="0")
    take_back_reason = Column(Text, nullable=True)
    # How the take-back landed, set when the refund succeeds: debited from the available balance / recorded as owed.
    take_back_debited_kobo = Column(BigInteger, nullable=True)
    take_back_owed_kobo = Column(BigInteger, nullable=True)
    failure_reason = Column(Text, nullable=True)  # Paystack's text on the current attempt
    processed_at = Column(DateTime, nullable=True)  # money confirmed out
    note = Column(Text, nullable=True)
    # Paystack contradicted what we recorded on a refund that cannot change status (finished, or the booking has another open
    # refund): the console queue shows these. A refund that CAN change goes to needs_review instead.
    review_flagged_at = Column(DateTime, nullable=True)
    review_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("Booking")
    attempts = relationship("RefundAttempt", order_by="RefundAttempt.attempt", back_populates="refund")

    @property
    def current_attempt(self):
        """The highest-numbered attempt, or None. Only this one can settle the refund."""
        return max(self.attempts, key=lambda a: a.attempt, default=None)


class RefundAttempt(Base):
    """One call to Paystack for a refund. A retry adds a row; nothing is overwritten."""
    __tablename__ = "refund_attempts"
    __table_args__ = (
        CheckConstraint(_in("status", REFUND_ATTEMPT_STATUSES), name="ck_refund_attempts_status"),
        UniqueConstraint("refund_id", "attempt", name="uq_refund_attempts_refund_attempt"),
        Index("uq_refund_attempts_paystack_refund_id", "paystack_refund_id", unique=True,
              postgresql_where=text("paystack_refund_id IS NOT NULL")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    refund_id = Column(UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=False)
    attempt = Column(Integer, nullable=False)
    paystack_refund_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default=ATTEMPT_SENDING)
    failure_reason = Column(Text, nullable=True)
    bank_details_submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    refund = relationship("Refund", back_populates="attempts")
