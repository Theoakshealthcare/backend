"""Caregiver payouts (A3 slice 2): a scheduled batch of bank transfers, one row per caregiver.

Money flow, all integer kobo. ``caregiver_wallets.balance`` is the AVAILABLE balance; the amount of a payout is taken out
of it when the payout is created (reserved), as a ``payout`` row in ``caregiver_transactions``. The two rows move together:

====================  =============================  ==========================================================
``payouts.status``    ``caregiver_transactions``     meaning
====================  =============================  ==========================================================
pending_approval      pending_approval               reserved, waiting for an admin to approve the batch
processing            pending                        approved; the transfer is queued, sent or on its way
success               successful                     Paystack paid it (``transfer.success``)
failed                failed                         Paystack could not pay it; the money is back in the balance
reversed              failed                         Paystack paid, then took it back; the money is back
cancelled             cancelled                      an admin (or the approval / send check) cancelled it; money is back
needs_attention       pending or successful          a person must look (see below); the caregiver is left out of new batches
====================  =============================  ==========================================================

``needs_attention`` means Paystack's word contradicted what we recorded (or Paystack has no record of a transfer we sent) and a
person must look. Entering it FREEZES the caregiver (``caregiver_wallets.payouts_frozen_at``): from then on automatic code
may only lower what the caregiver can be paid, never raise it, and money comes back only through an admin action (``resolve``,
``unfreeze``). See ``app/services/payouts.py`` for the invariant and the contradictions that trigger it.

Every send is a row in ``payout_attempts`` (``payouts.reference`` is the current attempt's); events from Paystack are matched
by attempt reference, so a late event for an earlier attempt is never lost.

Lock order, the same as the rest of the money code: booking, then the transaction rows, then the wallet. A payout is a
transaction-class row: batch, then payout, then its ledger row, then the wallet.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

PAYOUT_PENDING_APPROVAL, PAYOUT_PROCESSING, PAYOUT_SUCCESS = "pending_approval", "processing", "success"
PAYOUT_FAILED, PAYOUT_REVERSED, PAYOUT_CANCELLED = "failed", "reversed", "cancelled"
PAYOUT_NEEDS_ATTENTION = "needs_attention"
PAYOUT_STATUSES = (PAYOUT_PENDING_APPROVAL, PAYOUT_PROCESSING, PAYOUT_SUCCESS, PAYOUT_FAILED, PAYOUT_REVERSED,
                   PAYOUT_CANCELLED, PAYOUT_NEEDS_ATTENTION)
# A caregiver can have at most one of these at a time (a unique partial index enforces it). ``needs_attention`` is not one of
# them: it blocks new batches through the caregiver's freeze, not through this index.
PAYOUT_LIVE = (PAYOUT_PENDING_APPROVAL, PAYOUT_PROCESSING)
# Everything that is not finished.
PAYOUT_OPEN = PAYOUT_LIVE + (PAYOUT_NEEDS_ATTENTION,)

# payout_attempts.status: pending = created / sent / on its way; refused = Paystack answered with a definite no, so no transfer
# exists; the others are Paystack's final word on the transfer.
ATTEMPT_PENDING, ATTEMPT_SUCCESS, ATTEMPT_FAILED = "pending", "success", "failed"
ATTEMPT_REVERSED, ATTEMPT_REFUSED = "reversed", "refused"
ATTEMPT_STATUSES = (ATTEMPT_PENDING, ATTEMPT_SUCCESS, ATTEMPT_FAILED, ATTEMPT_REVERSED, ATTEMPT_REFUSED)

BATCH_PENDING_APPROVAL, BATCH_APPROVED, BATCH_CANCELLED = "pending_approval", "approved", "cancelled"
BATCH_STATUSES = (BATCH_PENDING_APPROVAL, BATCH_APPROVED, BATCH_CANCELLED)


class PayoutBatch(Base):
    __tablename__ = "payout_batches"
    __table_args__ = (
        CheckConstraint("status IN ('pending_approval','approved','cancelled')", name="ck_payout_batches_status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The payout day this batch is for (an ISO date). Unique: the scheduler can run twice and still make one batch.
    period_key = Column(String, nullable=False, unique=True)
    cadence = Column(String, nullable=False)  # the policy's payout_cadence when the batch was made
    min_payout_kobo = Column(BigInteger, nullable=False)  # the policy's payout_min_naira x 100 when the batch was made
    status = Column(String, nullable=False, default=BATCH_PENDING_APPROVAL)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approval_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    payouts = relationship("Payout", back_populates="batch")


class Payout(Base):
    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint("amount_kobo > 0", name="ck_payouts_amount_positive"),
        CheckConstraint("shortfall_kobo >= 0", name="ck_payouts_shortfall_not_negative"),
        CheckConstraint("returned_kobo >= 0 AND netted_kobo >= 0", name="ck_payouts_resolution_not_negative"),
        CheckConstraint("status IN ('pending_approval','processing','success','failed','reversed','cancelled',"
                        "'needs_attention')", name="ck_payouts_status"),
        # One live payout per caregiver: a second batch (or a retry) can never reserve the same money twice.
        Index("uq_payouts_one_active_per_caregiver", "caregiver_id", unique=True,
              postgresql_where=text("status IN ('pending_approval','processing')")),
        Index("ix_payouts_batch", "batch_id"),
        Index("ix_payouts_status", "status", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("payout_batches.id"), nullable=False)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    # The ledger row (caregiver_transactions.type = 'payout') that holds the reservation. One per payout.
    transaction_id = Column(UUID(as_uuid=True), ForeignKey("caregiver_transactions.id"), nullable=False, unique=True)
    amount_kobo = Column(BigInteger, nullable=False)
    status = Column(String, nullable=False, default=PAYOUT_PENDING_APPROVAL)
    # Where the money goes, copied from the caregiver's bank account when the payout was made (so a later change to the
    # account cannot redirect a payout that has been reserved).
    bank_name = Column(String, nullable=False)
    bank_code = Column(String, nullable=False)
    account_number = Column(String, nullable=False)
    account_name = Column(String, nullable=False)
    recipient_code = Column(String, nullable=False)
    # The Paystack transfer: the reference is ours and unique per attempt (Paystack refuses a reused one), which is what
    # makes initiating a transfer safe to repeat.
    reference = Column(String, nullable=False, unique=True)
    attempts = Column(Integer, nullable=False, default=1)  # the number of the current attempt (payout_attempts.attempt)
    # Money Paystack paid out that the caregiver's balance could not cover when it was taken back (see needs_attention). It
    # only ever goes down by an admin netting it off (resolve as failed) or at unfreeze (debited from the balance, or written off).
    shortfall_kobo = Column(BigInteger, nullable=False, default=0, server_default="0")
    # Set when an admin resolves the payout as failed: how much of its reservation went back to the balance, and how much was
    # kept against the caregiver's outstanding shortfall.
    returned_kobo = Column(BigInteger, nullable=False, default=0, server_default="0")
    netted_kobo = Column(BigInteger, nullable=False, default=0, server_default="0")
    transfer_code = Column(String, nullable=True)
    failure_reason = Column(Text, nullable=True)
    initiated_at = Column(DateTime, nullable=True)  # set when the send worker claims it, before the call to Paystack
    completed_at = Column(DateTime, nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    cancelled_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)  # null: cancelled by the system
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("PayoutBatch", back_populates="payouts")
    caregiver = relationship("Caregiver")
    attempt_rows = relationship("PayoutAttempt", order_by="PayoutAttempt.attempt")


class PayoutAttempt(Base):
    """One send of a payout to Paystack, with its own unique reference. A retry adds a row; nothing is overwritten, so a late
    event for an earlier attempt still finds its payout and the failure history is kept."""
    __tablename__ = "payout_attempts"
    __table_args__ = (
        CheckConstraint("status IN ('pending','success','failed','reversed','refused')", name="ck_payout_attempts_status"),
        UniqueConstraint("payout_id", "attempt", name="uq_payout_attempts_payout_attempt"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payout_id = Column(UUID(as_uuid=True), ForeignKey("payouts.id"), nullable=False, index=True)
    attempt = Column(Integer, nullable=False)
    reference = Column(String, nullable=False, unique=True)
    status = Column(String, nullable=False, default=ATTEMPT_PENDING)
    failure_reason = Column(Text, nullable=True)
    # True when an admin marked this attempt paid although Paystack's own answer was not final (resolve with ``force``): a later
    # failed or reversed event for it is then a contradiction to look at, not a stale duplicate.
    forced = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CaregiverBankEvent(Base):
    """Append-only history of a caregiver's bank account changes (the admin audit log is for admins; this is the caregiver's
    own action). Account numbers are stored masked to the last four digits."""
    __tablename__ = "caregiver_bank_events"
    __table_args__ = (Index("ix_caregiver_bank_events_caregiver", "caregiver_id", "created_at"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=False)
    action = Column(String, nullable=False)  # bank_account.set | bank_account.change
    before = Column(JSONB, nullable=True)
    after = Column(JSONB, nullable=True)
    payout_blocked_until = Column(DateTime, nullable=True)
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
