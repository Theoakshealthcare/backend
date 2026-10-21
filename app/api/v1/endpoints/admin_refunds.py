"""Admin: refunds (Refunds v2, spec section 8). Thin routes over ``app.services.refunds``, which holds every rule.

Reading the queue and a refund needs any of ``refunds.request`` / ``refunds.approve`` / ``payments.read``. Raising a refund needs
``refunds.request``; approving, rejecting, retrying, sending bank details and closing need ``refunds.approve``; switching a failed
refund to the wallet and resolving a refund in review need ``refunds.approve_above_limit`` (Super Admin). The service checks the
limits, the requester rule and the money rules again. Every change writes an ``admin_audit_log`` row. Money is naira here and
integer kobo below this layer."""

from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from app.api.deps import get_db, require_any_permission, require_permission
from app.core.money import kobo_to_naira, to_kobo
from app.db.models.admin import AdminUser
from app.db.models.booking import Booking, BookingStatus
from app.db.models.refund import (
    REFUND_COMMITTED_STATUSES, REFUND_FAILED, REFUND_NEEDS_ATTENTION, REFUND_NEEDS_REVIEW, REFUND_OPEN_STATUSES,
    REFUND_ORIGIN_STAFF, REFUND_PENDING_APPROVAL, REFUND_TERMINAL_STATUSES, Refund, RefundAttempt,
)
from app.schemas.admin_refunds import (
    AttemptOut, BookingContextOut, RefundApproveIn, RefundBankDetailsIn, RefundDetail, RefundList, RefundOut,
    RefundReasonCode, RefundReasonIn, RefundRequestIn, RefundResolveIn, RefundStatusFilter,
)
from app.services import booking_policy, refunds
from app.services.booking_cancellation import TIER_LATE, TIER_STANDARD, CancelPolicy, _fee
from app.services.paystack import PaystackService

router = APIRouter()

READ = ("refunds.request", "refunds.approve", "payments.read")
APPROVE_ABOVE_LIMIT = refunds.APPROVE_ABOVE_LIMIT


def get_paystack_service() -> PaystackService:
    return PaystackService()


# --- numbers ---------------------------------------------------------------------------------------------------------
def _naira(kobo: Optional[int]) -> Optional[float]:
    return None if kobo is None else float(kobo_to_naira(int(kobo)))


def _kobo(naira: Optional[Decimal]) -> Optional[int]:
    return None if naira is None else to_kobo(naira)


def _effective_kobo(refund: Refund) -> int:
    """The amount the refund is about: what was approved, else what is suggested (0 when neither is known)."""
    return int(refund.amount_kobo if refund.amount_kobo is not None else refund.suggested_amount_kobo or 0)


# --- what the caller may do (mirrors the service rules, so the console never offers something that 403s) -------------
def _actions(admin: AdminUser, refund: Refund, *, cumulative_kobo: int, limit_kobo: int, channel: str) -> List[str]:
    perms = admin.permissions
    can_approve = "refunds.approve" in perms
    above = APPROVE_ABOVE_LIMIT in perms
    state = refund.status
    out: List[str] = []
    if state == REFUND_PENDING_APPROVAL and can_approve:
        # RF-D11: the person who raised a staff refund cannot approve it (they can still reject, i.e. withdraw it).
        if not (refund.origin == REFUND_ORIGIN_STAFF and refund.requested_by_admin_id == admin.id):
            out.append("approve")
        out.append("reject")
    if state == REFUND_FAILED and can_approve:
        out.append("retry")
    if state == REFUND_NEEDS_ATTENTION and can_approve:
        # They redirect the money: not the approver or raiser, and above the Finance limit only a Super Admin.
        if (admin.id not in (refund.approved_by_admin_id, refund.requested_by_admin_id)
                and (cumulative_kobo <= limit_kobo or above)):
            out.append("bank_details")
    if state == REFUND_FAILED and above and channel == "app":
        out.append("switch_to_wallet")
    if state == REFUND_FAILED and can_approve:
        out.append("close")
    if (state == REFUND_NEEDS_REVIEW or refund.review_flagged_at is not None) and above:
        out.append("resolve")
    return out


def _refund_out(admin: AdminUser, refund: Refund, code: Optional[str], channel: str, others_kobo: int,
                limit_kobo: int) -> RefundOut:
    return RefundOut(
        id=refund.id, booking_id=refund.booking_id, booking_code=code, status=refund.status, reason_code=refund.reason_code,
        origin=refund.origin, suggested_amount_naira=_naira(refund.suggested_amount_kobo),
        amount_naira=_naira(refund.amount_kobo), destination=refund.destination,
        take_back_from_caregiver=bool(refund.take_back_from_caregiver), take_back_naira=_naira(refund.take_back_kobo or 0),
        failure_reason=refund.failure_reason, note=refund.note, rejected_reason=refund.rejected_reason,
        requested_by=refund.requested_by_admin_id, approved_by=refund.approved_by_admin_id,
        review_flagged_at=refund.review_flagged_at, review_reason=refund.review_reason, created_at=refund.created_at,
        approved_at=refund.approved_at, processed_at=refund.processed_at,
        available_actions=_actions(admin, refund, cumulative_kobo=_effective_kobo(refund) + others_kobo,
                                   limit_kobo=limit_kobo, channel=channel))


# --- queries ---------------------------------------------------------------------------------------------------------
def _rows_query(db: Session):
    """Refunds with their booking's code and channel, and Σ amount of the booking's OTHER committed refunds (the cumulative
    base of the Finance limit) as ``others_kobo``. Returns the query and that column."""
    other = aliased(Refund)
    others = (db.query(func.coalesce(func.sum(other.amount_kobo), 0))
              .filter(other.booking_id == Refund.booking_id, other.id != Refund.id,
                      other.status.in_(REFUND_COMMITTED_STATUSES)).correlate(Refund).scalar_subquery())
    query = (db.query(Refund, Booking.booking_code, Booking.channel, others.label("others_kobo"))
             .join(Booking, Booking.id == Refund.booking_id))
    return query, others


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except refunds.RefundNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=exc.message)
    except refunds.RefundConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=exc.message)
    except refunds.RefundForbidden as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=exc.message)
    except refunds.RefundInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message)
    except refunds.RefundPaystackUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.message)


def _suggestion_why(refund: Refund, booking: Booking) -> str:
    code, tier = refund.reason_code, booking.cancel_fee_tier
    if refund.suggested_amount_kobo is None:
        return "No amount was suggested. Enter the amount to refund."
    if refund.origin == REFUND_ORIGIN_STAFF:
        return "The amount entered when this refund was raised."
    if code == "cancelled_by_booker":
        if tier in (TIER_STANDARD, TIER_LATE):
            return f"The booker cancelled in the {tier} tier, so the suggestion is the total less the cancellation fee."
        return "The booker cancelled free of charge, so the suggestion is the full amount."
    if code == "admin_cancelled":
        return "Set by the admin who cancelled the booking."
    return "The booking could not go ahead through no fault of the family, so the suggestion is the full amount."


def _cancel_fee_kobo(booking: Booking) -> Optional[int]:
    tier = booking.cancel_fee_tier
    if tier == "none":
        return 0
    policy = CancelPolicy.from_snapshot(booking.cancel_policy)
    if tier in (TIER_STANDARD, TIER_LATE) and policy is not None:
        return min(_fee(booking, policy.percent(tier)), int(booking.total_amount_kobo))
    return None


def _detail(db: Session, admin: AdminUser, refund_id: UUID) -> RefundDetail:
    query, _others = _rows_query(db)
    row = query.filter(Refund.id == refund_id).populate_existing().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Refund not found")
    refund, code, channel, others = row
    booking = db.get(Booking, refund.booking_id)
    limit_kobo = booking_policy.current(db).finance_refund_limit_naira * 100
    base = _refund_out(admin, refund, code, channel, int(others), limit_kobo)
    attempts = db.query(RefundAttempt).filter(RefundAttempt.refund_id == refund.id).order_by(RefundAttempt.attempt).all()
    effective = _effective_kobo(refund)
    total = int(booking.total_amount_kobo)
    refundable = refunds.refundable_kobo(db, booking)
    context = BookingContextOut(
        code=booking.booking_code, status=booking.status.value, channel=booking.channel,
        cancel_fee_tier=booking.cancel_fee_tier, cancel_fee_naira=_naira(_cancel_fee_kobo(booking)),
        caregiver_assigned=booking.caregiver_id is not None, caregiver_arrived=booking.actual_start_time is not None,
        completed=booking.status == BookingStatus.COMPLETED, total_naira=_naira(total),
        paystack_fee_naira=_naira(booking.paystack_fee_kobo), refunded_naira=_naira(max(0, total - refundable)),
        refundable_naira=_naira(refundable))
    finished = refund.status in REFUND_TERMINAL_STATUSES
    return RefundDetail(
        **base.model_dump(), booking=context, suggestion_why=_suggestion_why(refund, booking),
        attempts=[AttemptOut(attempt=a.attempt, status=a.status, failure_reason=a.failure_reason,
                             paystack_refund_id=a.paystack_refund_id, bank_code=a.bank_code, account_last4=a.account_last4,
                             account_name=a.account_name, bank_details_submitted_at=a.bank_details_submitted_at,
                             created_at=a.created_at) for a in attempts],
        take_back_reason=refund.take_back_reason,
        take_back_preview_naira=(None if finished or not effective
                                 else _naira(refunds.take_back_preview_kobo(db, refund, effective))),
        take_back_debited_naira=_naira(refund.take_back_debited_kobo), take_back_owed_naira=_naira(refund.take_back_owed_kobo),
        needs_super_admin=effective + int(others) > limit_kobo, limit_naira=_naira(limit_kobo))


def _after_change(db: Session, admin: AdminUser, refund_id: UUID) -> RefundDetail:
    db.expire_all()  # the service committed: read what is really stored
    return _detail(db, admin, refund_id)


# --- reads -----------------------------------------------------------------------------------------------------------
@router.get("", response_model=RefundList)
def list_refunds(
    status_: Optional[RefundStatusFilter] = Query(None, alias="status", description="Default: open (unfinished refunds, "
                                                  "plus finished ones flagged for review). 'all' removes the filter."),
    reason_code: Optional[RefundReasonCode] = None, booking_id: Optional[UUID] = None,
    above_limit: Optional[bool] = Query(None, description="Only refunds whose booking's refunds total above (true) or "
                                                          "within (false) the Finance limit"),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_any_permission(*READ)),
):
    query, others = _rows_query(db)
    if status_ in (None, "open"):
        query = query.filter(or_(Refund.status.in_(REFUND_OPEN_STATUSES), Refund.review_flagged_at.isnot(None)))
    elif status_ != "all":
        query = query.filter(Refund.status == status_)
    if reason_code:
        query = query.filter(Refund.reason_code == reason_code)
    if booking_id:
        query = query.filter(Refund.booking_id == booking_id)
    limit_kobo = booking_policy.current(db).finance_refund_limit_naira * 100
    if above_limit is not None:
        cumulative = func.coalesce(Refund.amount_kobo, Refund.suggested_amount_kobo, 0) + others
        query = query.filter(cumulative > limit_kobo if above_limit else cumulative <= limit_kobo)
    total = query.count()
    rows = query.order_by(Refund.created_at.desc(), Refund.id).limit(limit).offset(offset).all()
    return RefundList(items=[_refund_out(admin, r, code, channel, int(o), limit_kobo) for r, code, channel, o in rows],
                      total=total, limit=limit, offset=offset)


@router.get("/{refund_id}", response_model=RefundDetail)
def get_refund(refund_id: UUID, db: Session = Depends(get_db), admin: AdminUser = Depends(require_any_permission(*READ))):
    return _detail(db, admin, refund_id)


# --- writes ----------------------------------------------------------------------------------------------------------
@router.post("", response_model=RefundDetail, status_code=status.HTTP_201_CREATED)
def request_refund(body: RefundRequestIn, request: Request, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(require_permission("refunds.request"))):
    """Raise a refund by hand (a complaint, or anything else). It waits for someone else to approve it."""
    refund = _guard(refunds.request_refund, db, booking_id=body.booking_id, amount_kobo=_kobo(body.amount_naira),
                    reason_code=body.reason_code, note=body.note, admin=admin, request=request)
    return _after_change(db, admin, refund.id)


@router.post("/{refund_id}/approve", response_model=RefundDetail)
def approve_refund(refund_id: UUID, body: RefundApproveIn, request: Request, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(require_permission("refunds.approve"))):
    """Approve a pending refund. Above the Finance limit, or to the wallet, a Super Admin is needed."""
    _guard(refunds.approve_refund, db, refund_id, admin=admin, amount_kobo=_kobo(body.amount_naira),
           destination=body.destination, take_back_from_caregiver=body.take_back_from_caregiver,
           take_back_reason=body.take_back_reason, note=body.note, request=request)
    return _after_change(db, admin, refund_id)


@router.post("/{refund_id}/reject", response_model=RefundDetail)
def reject_refund(refund_id: UUID, body: RefundReasonIn, request: Request, db: Session = Depends(get_db),
                  admin: AdminUser = Depends(require_permission("refunds.approve"))):
    """Reject a pending refund; it is finished and the booking can be refunded again."""
    _guard(refunds.reject_refund, db, refund_id, admin=admin, reason=body.reason, request=request)
    return _after_change(db, admin, refund_id)


@router.post("/{refund_id}/retry", response_model=RefundDetail)
def retry_refund(refund_id: UUID, request: Request, db: Session = Depends(get_db),
                 admin: AdminUser = Depends(require_permission("refunds.approve")),
                 paystack: PaystackService = Depends(get_paystack_service)):
    """Send a failed refund again, once Paystack confirms the last attempt failed."""
    _guard(refunds.retry_refund, db, refund_id, admin=admin, paystack=paystack, request=request)
    return _after_change(db, admin, refund_id)


@router.post("/{refund_id}/bank-details", response_model=RefundDetail)
def submit_bank_details(refund_id: UUID, body: RefundBankDetailsIn, request: Request, db: Session = Depends(get_db),
                        admin: AdminUser = Depends(require_permission("refunds.approve")),
                        paystack: PaystackService = Depends(get_paystack_service)):
    """Give Paystack the family's bank account for a refund that needs it. The account number is never stored."""
    _guard(refunds.submit_bank_details, db, refund_id, admin=admin, bank_code=body.bank_code,
           account_number=body.account_number, paystack=paystack, request=request)
    return _after_change(db, admin, refund_id)


@router.post("/{refund_id}/switch-to-wallet", response_model=RefundDetail)
def switch_to_wallet(refund_id: UUID, request: Request, db: Session = Depends(get_db),
                     admin: AdminUser = Depends(require_permission(APPROVE_ABOVE_LIMIT)),
                     paystack: PaystackService = Depends(get_paystack_service)):
    """Pay a failed refund to the patient's Oakly wallet instead (Super Admin)."""
    _guard(refunds.switch_to_wallet, db, refund_id, admin=admin, paystack=paystack, request=request)
    return _after_change(db, admin, refund_id)


@router.post("/{refund_id}/close", response_model=RefundDetail)
def close_refund(refund_id: UUID, body: RefundReasonIn, request: Request, db: Session = Depends(get_db),
                 admin: AdminUser = Depends(require_permission("refunds.approve")),
                 paystack: PaystackService = Depends(get_paystack_service)):
    """Give up on a failed refund: the money stays with Oakly and the booking can be refunded again."""
    _guard(refunds.close_refund, db, refund_id, admin=admin, reason=body.reason, paystack=paystack, request=request)
    return _after_change(db, admin, refund_id)


@router.post("/{refund_id}/resolve", response_model=RefundDetail)
def resolve_refund(refund_id: UUID, body: RefundResolveIn, request: Request, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(require_permission(APPROVE_ABOVE_LIMIT)),
                   paystack: PaystackService = Depends(get_paystack_service)):
    """Settle a refund in review as succeeded or failed after checking the Paystack dashboard (Super Admin)."""
    _guard(refunds.resolve_refund, db, refund_id, admin=admin, outcome=body.outcome, note=body.note,
           amount_kobo=_kobo(body.amount_naira), paystack=paystack, request=request)
    return _after_change(db, admin, refund_id)
