"""Admin: caregiver earnings (AD-FR-38). Read with ``payments.read``; hold or release a hold with
``ledger.adjust_request`` (Finance), except that releasing the hold of a no-show-flagged booking also needs ``ledger.adjust``
(checked in the service, since it depends on the booking); void with ``ledger.adjust`` (Super Admin only, it writes an earning off). Every
change needs a reason and writes an ``admin_audit_log`` row. A released earning cannot be changed here."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.db.models.admin import AdminUser
from app.db.models.booking import Booking
from app.db.models.caregiver import EARNING_TYPES, Caregiver, CaregiverTransaction
from app.schemas.admin_earning import EarningList, EarningOut, EarningReasonIn, EarningStatusName
from app.services import earnings

router = APIRouter()


def _out(tx: CaregiverTransaction, caregiver_name: Optional[str], booking_code: Optional[str]) -> EarningOut:
    return EarningOut(
        id=tx.id, type=tx.type, status=tx.status, amount_kobo=tx.amount, base_amount_kobo=tx.base_amount_kobo,
        share_percent=tx.share_percent, caregiver_id=tx.caregiver_id, caregiver_name=caregiver_name,
        booking_id=tx.booking_id, booking_code=booking_code, available_at=tx.available_at, released_at=tx.released_at,
        status_reason=tx.status_reason, status_changed_by=tx.status_changed_by, status_changed_at=tx.status_changed_at,
        created_at=tx.created_at)


def _one(db: Session, tx_id: UUID) -> EarningOut:
    row = (db.query(CaregiverTransaction, Caregiver.full_name, Booking.booking_code)
           .join(Caregiver, Caregiver.id == CaregiverTransaction.caregiver_id)
           .outerjoin(Booking, Booking.id == CaregiverTransaction.booking_id)
           .filter(CaregiverTransaction.id == tx_id).one())
    return _out(*row)


def _act(db: Session, fn, tx_id: UUID, admin: AdminUser, body: EarningReasonIn, request: Request, **extra) -> EarningOut:
    try:
        fn(db, tx_id, admin_id=admin.id, reason=body.reason, request=request, **extra)
    except earnings.EarningForbidden as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc))
    except earnings.EarningNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Earning not found")
    except earnings.EarningConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return _one(db, tx_id)


@router.get("", response_model=EarningList)
def list_earnings(
    caregiver_id: Optional[UUID] = None, booking_id: Optional[UUID] = None, status_: Optional[EarningStatusName] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), _admin: AdminUser = Depends(require_permission("payments.read")),
):
    query = (db.query(CaregiverTransaction, Caregiver.full_name, Booking.booking_code)
             .join(Caregiver, Caregiver.id == CaregiverTransaction.caregiver_id)
             .outerjoin(Booking, Booking.id == CaregiverTransaction.booking_id)
             .filter(CaregiverTransaction.type.in_(EARNING_TYPES)))
    if caregiver_id:
        query = query.filter(CaregiverTransaction.caregiver_id == caregiver_id)
    if booking_id:
        query = query.filter(CaregiverTransaction.booking_id == booking_id)
    if status_:
        query = query.filter(CaregiverTransaction.status == status_)
    total = query.count()
    rows = query.order_by(CaregiverTransaction.created_at.desc(), CaregiverTransaction.id).limit(limit).offset(offset).all()
    return EarningList(items=[_out(*row) for row in rows], total=total)


@router.post("/{earning_id}/hold", response_model=EarningOut)
def hold_earning(earning_id: UUID, body: EarningReasonIn, request: Request, db: Session = Depends(get_db),
                 admin: AdminUser = Depends(require_permission("ledger.adjust_request"))):
    return _act(db, earnings.hold, earning_id, admin, body, request)


@router.post("/{earning_id}/release-hold", response_model=EarningOut)
def release_hold(earning_id: UUID, body: EarningReasonIn, request: Request, db: Session = Depends(get_db),
                 admin: AdminUser = Depends(require_permission("ledger.adjust_request"))):
    return _act(db, earnings.release_hold, earning_id, admin, body, request,
                can_clear_no_show=admin.has_permission("ledger.adjust"))


@router.post("/{earning_id}/void", response_model=EarningOut)
def void_earning(earning_id: UUID, body: EarningReasonIn, request: Request, db: Session = Depends(get_db),
                 admin: AdminUser = Depends(require_permission("ledger.adjust"))):
    return _act(db, earnings.void, earning_id, admin, body, request)
