"""Admin refunds (queue, detail, request, approve, reject, retry, bank details, switch to wallet, close, resolve): request and
response models. Money is naira at this edge (a number with at most 2 decimals going in, a 2dp number coming out); the service
and the database hold integer kobo."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

RefundStatusName = Literal["pending_approval", "approved", "processing", "needs_attention", "needs_review", "failed",
                           "succeeded", "rejected", "closed"]
# "open" is the queue default (every unfinished refund, plus finished ones flagged for review); "all" has no status filter.
RefundStatusFilter = Literal["open", "all", "pending_approval", "approved", "processing", "needs_attention", "needs_review",
                             "failed", "succeeded", "rejected", "closed"]
RefundReasonCode = Literal["cancelled_by_booker", "caregiver_cancelled", "no_caregiver_found", "admin_cancelled",
                           "paid_after_expiry", "paid_after_cancellation", "complaint", "other"]
RefundActionName = Literal["approve", "reject", "retry", "bank_details", "switch_to_wallet", "close", "resolve"]

MAX_NAIRA = Decimal("1000000000")  # N1bn: a typo guard, far above any booking


def _text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


Text = Annotated[str, AfterValidator(_text)]  # stripped, and not blank


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefundRequestIn(_In):
    booking_id: UUID
    amount_naira: Decimal = Field(gt=0, le=MAX_NAIRA, decimal_places=2)
    reason_code: RefundReasonCode
    note: Optional[str] = Field(None, max_length=2000)


class RefundApproveIn(_In):
    amount_naira: Optional[Decimal] = Field(None, gt=0, le=MAX_NAIRA, decimal_places=2,
                                            description="Omit to approve the suggested amount")
    destination: Literal["original_method", "wallet"] = "original_method"
    take_back_from_caregiver: bool = False
    take_back_reason: Optional[str] = Field(None, max_length=2000)
    note: Optional[str] = Field(None, max_length=2000)


class RefundReasonIn(_In):
    reason: Text = Field(max_length=2000)


class RefundBankDetailsIn(_In):
    bank_code: Text = Field(max_length=20)
    account_number: str = Field(pattern=r"^\d{10}$", description="Sent to Paystack and never stored")


class RefundResolveIn(_In):
    outcome: Literal["succeeded", "failed"]
    note: Text = Field(max_length=2000)
    amount_naira: Optional[Decimal] = Field(None, gt=0, le=MAX_NAIRA, decimal_places=2,
                                            description="Only to record a different amount than the refund's, when Paystack "
                                                        "processed that amount (the 409 names it)")


class AttemptOut(BaseModel):
    attempt: int
    status: str
    failure_reason: Optional[str]
    paystack_refund_id: Optional[str]
    bank_code: Optional[str]
    account_last4: Optional[str] = Field(description="The last 4 digits only; the full number is never stored")
    account_name: Optional[str]
    bank_details_submitted_at: Optional[datetime]
    created_at: datetime


class BookingContextOut(BaseModel):
    code: Optional[str]
    status: str
    channel: str
    cancel_fee_tier: Optional[str]
    cancel_fee_naira: Optional[float] = Field(description="Null when the booking has no policy copy to work it out from")
    caregiver_assigned: bool
    caregiver_arrived: bool
    completed: bool
    total_naira: float
    paystack_fee_naira: Optional[float] = Field(description="Null when Paystack's fee was never recorded (older bookings)")
    refunded_naira: float = Field(description="Committed to refunds of this booking (approved or further on, this one included)")
    refundable_naira: float


class RefundOut(BaseModel):
    id: UUID
    booking_id: UUID
    booking_code: Optional[str]
    status: RefundStatusName
    reason_code: str
    origin: Literal["system", "staff"]
    suggested_amount_naira: Optional[float]
    amount_naira: Optional[float]
    destination: Optional[str]
    take_back_from_caregiver: bool
    take_back_naira: float
    failure_reason: Optional[str]
    note: Optional[str]
    rejected_reason: Optional[str]
    requested_by: Optional[UUID]
    approved_by: Optional[UUID]
    review_flagged_at: Optional[datetime]
    review_reason: Optional[str]
    created_at: datetime
    approved_at: Optional[datetime]
    processed_at: Optional[datetime]
    max_approvable_naira: float = Field(description="The most this caller may approve: what is left to refund and, without "
                                                    "refunds.approve_above_limit, what is left under the Finance limit")
    available_actions: List[RefundActionName]


class RefundDetail(RefundOut):
    booking: BookingContextOut
    suggestion_why: str
    attempts: List[AttemptOut]
    take_back_reason: Optional[str]
    take_back_preview_naira: Optional[float] = Field(description="What taking the caregiver's share back would be at this "
                                                                  "refund's amount; null once the refund is finished")
    take_back_debited_naira: Optional[float]
    take_back_owed_naira: Optional[float]
    needs_super_admin: bool = Field(description="This refund's amount is more than this caller may approve (see max_approvable_naira)")
    limit_naira: float


class RefundList(BaseModel):
    items: List[RefundOut]
    total: int
    limit: int
    offset: int
