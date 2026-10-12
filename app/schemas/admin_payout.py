"""Admin caregiver payouts (batches, approve, cancel, retry, resolve, unfreeze): request and response models."""

from datetime import datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

PayoutStatusName = Literal["pending_approval", "processing", "success", "failed", "reversed", "cancelled", "needs_attention"]
AttemptStatusName = Literal["pending", "success", "failed", "reversed", "refused"]
BatchStatusName = Literal["pending_approval", "approved", "cancelled"]


class PayoutReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(max_length=2000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class PayoutResolveIn(PayoutReasonIn):
    outcome: Literal["paid", "failed"]
    force: bool = Field(False, description="Resolve although Paystack's answer is not final (pending, OTP, no record, "
                                           "unreachable). Never overrides a definite answer that contradicts the outcome.")


class PayoutUnfreezeIn(PayoutReasonIn):
    write_off_shortfall: bool = Field(False, description="Write off the shortfall that the caregiver's available balance cannot "
                                                         "cover (money Paystack paid beyond their earnings). The balance is "
                                                         "debited first; without this flag a shortfall that is left refuses "
                                                         "the unfreeze (409).")


class UnfreezeOut(BaseModel):
    caregiver_id: UUID
    payouts_frozen_at: Optional[datetime] = Field(description="Null: the caregiver can be paid again")
    debited_from_balance_kobo: int = Field(description="Taken from the caregiver's available balance against the shortfall")
    written_off_shortfall_kobo: int
    outstanding_shortfall_kobo: int = Field(description="Shortfall still recorded on the caregiver's payouts (always 0: what "
                                                         "the balance cannot cover is written off or the unfreeze is refused)")


class AttemptOut(BaseModel):
    attempt: int
    reference: str
    status: AttemptStatusName
    failure_reason: Optional[str]
    created_at: datetime


class PayoutOut(BaseModel):
    id: UUID
    batch_id: UUID
    batch_period: Optional[str] = Field(description="The payout day of the batch (an ISO date)")
    caregiver_id: UUID
    caregiver_name: Optional[str]
    amount_kobo: int
    status: PayoutStatusName
    bank_name: str
    account_number: str = Field(description="Masked to the last four digits")
    account_name: str
    reference: str = Field(description="Our unique Paystack reference for the current attempt")
    transfer_code: Optional[str] = Field(description="Paystack's code once the transfer exists")
    attempts: int = Field(description="The number of the current attempt")
    attempt_history: List[AttemptOut] = Field(description="Every send of this payout, oldest first, with its failure reason")
    shortfall_kobo: int = Field(description="Paid out by Paystack but not recoverable from the caregiver's balance. Goes down only "
                                            "when an admin nets it off (resolve as failed), or at unfreeze, when it is debited from the "
                                            "caregiver's balance or written off")
    returned_kobo: int = Field(description="Set by resolving as failed: how much of the reservation went back to the balance")
    netted_kobo: int = Field(description="Set by resolving as failed: how much was kept against the caregiver's shortfall")
    caregiver_payouts_frozen_at: Optional[datetime] = Field(
        description="The caregiver-level freeze (the same on every payout of the caregiver); null when they can be paid")
    caregiver_payouts_frozen_reason: Optional[str] = Field(description="What started the freeze")
    failure_reason: Optional[str] = Field(description="Why it failed, or a note that needs a person (for example the OTP message)")
    initiated_at: Optional[datetime] = Field(description="When the send job claimed it to call Paystack; null until then")
    completed_at: Optional[datetime]
    approved_by: Optional[UUID]
    approved_at: Optional[datetime]
    cancelled_by: Optional[UUID] = Field(description="Null when the system cancelled it at approval")
    cancelled_at: Optional[datetime]
    cancel_reason: Optional[str]
    created_at: datetime


class PayoutList(BaseModel):
    items: List[PayoutOut]
    total: int


class BatchOut(BaseModel):
    id: UUID
    period_key: str
    cadence: str
    min_payout_kobo: int
    status: BatchStatusName
    approved_by: Optional[UUID]
    approved_at: Optional[datetime]
    approval_reason: Optional[str]
    created_at: datetime
    payout_count: int
    total_kobo: int = Field(description="Sum of the payouts that are not cancelled")
    counts: Dict[str, int] = Field(description="Number of payouts in each status")


class BatchDetail(BatchOut):
    payouts: List[PayoutOut]


class BatchList(BaseModel):
    items: List[BatchOut]
    total: int
