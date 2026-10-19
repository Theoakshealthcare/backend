"""Admin: caregiver payouts (A3 slice 2). Money leaves Oakly through these routes. Approving a batch and cancelling a batch or a payout need
``payouts.execute`` (Finance and Super Admin); retrying, resolving (including ``force``) and unfreezing a caregiver need
``ledger.adjust`` (Super Admin only); reading needs ``payments.read``. Every change needs a reason and writes
an ``admin_audit_log`` row. Batches are created by the scheduled job, never by a route."""

from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.db.models.admin import AdminUser
from app.db.models.caregiver import Caregiver, CaregiverWallet
from app.db.models.payout import PAYOUT_CANCELLED, Payout, PayoutAttempt, PayoutBatch
from app.schemas.admin_payout import (
    AttemptOut,
    BatchDetail,
    BatchList,
    BatchOut,
    BatchStatusName,
    PayoutList,
    PayoutOut,
    PayoutReasonIn,
    PayoutResolveIn,
    PayoutStatusName,
    PayoutUnfreezeIn,
    UnfreezeOut,
)
from app.services import payouts
from app.services.bank_accounts import mask

router = APIRouter()


def _payout_out(p: Payout, caregiver_name: Optional[str], batch_period: Optional[str],
                attempts: Optional[List[PayoutAttempt]] = None, frozen_at=None, frozen_reason: Optional[str] = None) -> PayoutOut:
    return PayoutOut(
        id=p.id, batch_id=p.batch_id, batch_period=batch_period, caregiver_id=p.caregiver_id, caregiver_name=caregiver_name,
        amount_kobo=p.amount_kobo, status=p.status, bank_name=p.bank_name, account_number=mask(p.account_number) or "",
        account_name=p.account_name, reference=p.reference, transfer_code=p.transfer_code, attempts=p.attempts,
        attempt_history=[AttemptOut(attempt=a.attempt, reference=a.reference, status=a.status,
                                    failure_reason=a.failure_reason, created_at=a.created_at) for a in attempts or []],
        shortfall_kobo=p.shortfall_kobo, returned_kobo=p.returned_kobo, netted_kobo=p.netted_kobo,
        caregiver_payouts_frozen_at=frozen_at, caregiver_payouts_frozen_reason=frozen_reason, failure_reason=p.failure_reason, initiated_at=p.initiated_at,
        completed_at=p.completed_at, approved_by=p.approved_by, approved_at=p.approved_at, cancelled_by=p.cancelled_by,
        cancelled_at=p.cancelled_at, cancel_reason=p.cancel_reason, created_at=p.created_at)


def _payouts_out(db: Session, rows) -> List[PayoutOut]:
    """Rows of (Payout, caregiver name, batch period, freeze time, freeze reason) as responses, with every payout's attempts
    loaded in one query."""
    rows = list(rows)
    by_payout: Dict[UUID, List[PayoutAttempt]] = {}
    if rows:
        for attempt in (db.query(PayoutAttempt).filter(PayoutAttempt.payout_id.in_([r[0].id for r in rows]))
                        .order_by(PayoutAttempt.attempt).all()):
            by_payout.setdefault(attempt.payout_id, []).append(attempt)
    return [_payout_out(p, name, period, by_payout.get(p.id), frozen_at, frozen_reason)
            for p, name, period, frozen_at, frozen_reason in rows]


def _payout_query(db: Session):
    return (db.query(Payout, Caregiver.full_name, PayoutBatch.period_key, CaregiverWallet.payouts_frozen_at,
                     CaregiverWallet.payouts_frozen_reason)
            .join(Caregiver, Caregiver.id == Payout.caregiver_id)
            .join(PayoutBatch, PayoutBatch.id == Payout.batch_id)
            .outerjoin(CaregiverWallet, CaregiverWallet.caregiver_id == Payout.caregiver_id))


def _one_payout(db: Session, payout_id: UUID) -> PayoutOut:
    return _payouts_out(db, [_payout_query(db).filter(Payout.id == payout_id).one()])[0]


def _batch_out(db: Session, batch: PayoutBatch) -> BatchOut:
    counts: Dict[str, int] = {}
    total = 0
    for payout_status, count, amount in (db.query(Payout.status, func.count(Payout.id), func.coalesce(func.sum(Payout.amount_kobo), 0))
                                         .filter(Payout.batch_id == batch.id).group_by(Payout.status).all()):
        counts[payout_status] = int(count)
        if payout_status != PAYOUT_CANCELLED:
            total += int(amount)
    return BatchOut(
        id=batch.id, period_key=batch.period_key, cadence=batch.cadence, min_payout_kobo=batch.min_payout_kobo,
        status=batch.status, approved_by=batch.approved_by, approved_at=batch.approved_at,
        approval_reason=batch.approval_reason, created_at=batch.created_at, payout_count=sum(counts.values()),
        total_kobo=total, counts=counts)


def _guard(fn, *args, not_found: str = "Payout not found", **kwargs):
    try:
        return fn(*args, **kwargs)
    except payouts.PayoutNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=not_found)
    except payouts.PayoutConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/batches", response_model=BatchList)
def list_batches(
    status_: Optional[BatchStatusName] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), _admin: AdminUser = Depends(require_permission("payments.read")),
):
    query = db.query(PayoutBatch)
    if status_:
        query = query.filter(PayoutBatch.status == status_)
    total = query.count()
    rows = query.order_by(PayoutBatch.created_at.desc(), PayoutBatch.id).limit(limit).offset(offset).all()
    return BatchList(items=[_batch_out(db, b) for b in rows], total=total)


@router.get("/batches/{batch_id}", response_model=BatchDetail)
def get_batch(batch_id: UUID, db: Session = Depends(get_db), _admin: AdminUser = Depends(require_permission("payments.read"))):
    batch = db.get(PayoutBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Payout batch not found")
    rows = _payout_query(db).filter(Payout.batch_id == batch_id).order_by(Payout.created_at, Payout.id).all()
    return BatchDetail(**_batch_out(db, batch).model_dump(), payouts=_payouts_out(db, rows))


@router.post("/batches/{batch_id}/approve", response_model=BatchDetail)
def approve_batch(batch_id: UUID, body: PayoutReasonIn, request: Request, db: Session = Depends(get_db),
                  admin: AdminUser = Depends(require_permission("payouts.execute"))):
    """Approve every payout still waiting in the batch. The send job then pays them through Paystack."""
    try:
        payouts.approve_batch(db, batch_id, admin_id=admin.id, reason=body.reason, request=request)
    except payouts.PayoutNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Payout batch not found")
    except payouts.PayoutConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return get_batch(batch_id, db, admin)


@router.post("/batches/{batch_id}/cancel", response_model=BatchDetail)
def cancel_batch(batch_id: UUID, body: PayoutReasonIn, request: Request, db: Session = Depends(get_db),
                 admin: AdminUser = Depends(require_permission("payouts.execute"))):
    """Cancel a batch that has not been approved: every payout in it is cancelled and its money returned."""
    _guard(payouts.cancel_batch, db, batch_id, admin_id=admin.id, reason=body.reason, request=request,
           not_found="Payout batch not found")
    return get_batch(batch_id, db, admin)


@router.get("", response_model=PayoutList)
def list_payouts(
    batch_id: Optional[UUID] = None, caregiver_id: Optional[UUID] = None,
    status_: Optional[PayoutStatusName] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), _admin: AdminUser = Depends(require_permission("payments.read")),
):
    query = _payout_query(db)
    if batch_id:
        query = query.filter(Payout.batch_id == batch_id)
    if caregiver_id:
        query = query.filter(Payout.caregiver_id == caregiver_id)
    if status_:
        query = query.filter(Payout.status == status_)
    total = query.count()
    rows = query.order_by(Payout.created_at.desc(), Payout.id).limit(limit).offset(offset).all()
    return PayoutList(items=_payouts_out(db, rows), total=total)


@router.post("/{payout_id}/cancel", response_model=PayoutOut)
def cancel_payout(payout_id: UUID, body: PayoutReasonIn, request: Request, db: Session = Depends(get_db),
                  admin: AdminUser = Depends(require_permission("payouts.execute"))):
    """Cancel a payout that has not been approved; the reserved money goes back to the caregiver's available balance."""
    _guard(payouts.cancel_payout, db, payout_id, admin_id=admin.id, reason=body.reason, request=request)
    return _one_payout(db, payout_id)


@router.post("/{payout_id}/retry", response_model=PayoutOut)
def retry_payout(payout_id: UUID, body: PayoutReasonIn, request: Request, db: Session = Depends(get_db),
                 admin: AdminUser = Depends(require_permission("ledger.adjust"))):
    """Send a failed or reversed payout again (the money is reserved again; the send job pays it)."""
    _guard(payouts.retry_payout, db, payout_id, admin_id=admin.id, reason=body.reason, request=request)
    return _one_payout(db, payout_id)


@router.post("/{payout_id}/resolve", response_model=PayoutOut)
def resolve_payout(payout_id: UUID, body: PayoutResolveIn, request: Request, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(require_permission("ledger.adjust"))):
    """Settle a payout Paystack's own word has not (``processing``, for example stuck at OTP, or ``needs_attention``) as
    ``paid`` or ``failed`` (the reserved money is returned). Paystack is asked first; an answer that contradicts the outcome
    refuses it, and an answer that is not final needs ``force: true``."""
    _guard(payouts.resolve_payout, db, payout_id, outcome=body.outcome, reason=body.reason, force=body.force,
           admin_id=admin.id, request=request)
    return _one_payout(db, payout_id)


@router.post("/caregivers/{caregiver_id}/unfreeze", response_model=UnfreezeOut)
def unfreeze_caregiver(caregiver_id: UUID, body: PayoutUnfreezeIn, request: Request, db: Session = Depends(get_db),
                       admin: AdminUser = Depends(require_permission("ledger.adjust"))):
    """Let a caregiver whose payouts were frozen be paid again. Refused while any of their payouts still needs attention or is
    processing. A shortfall is first debited from the caregiver's available balance; what the balance cannot cover refuses the
    unfreeze (409) unless ``write_off_shortfall`` is true."""
    return _guard(payouts.unfreeze_caregiver, db, caregiver_id, admin_id=admin.id, reason=body.reason,
                  write_off_shortfall=body.write_off_shortfall, request=request, not_found="Caregiver not found")
