"""Admin: the cancellation fee tiers and caregiver drop-out timings (``/admin/booking-policy``).

Reading needs ``bookings.read`` (support wants to quote the policy); changing it needs ``settings.write`` and a reason,
and writes an ``admin_audit_log`` row with the old and new values. Editing never changes a booking already quoted: the
cancellation tiers are copied onto each quote and booking when they are made.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.db.models.admin import AdminUser
from app.schemas.booking_policy import BookingPolicyOut, BookingPolicyPatch
from app.services import booking_policy

router = APIRouter()


def _out(db: Session) -> BookingPolicyOut:
    row = booking_policy.get_row(db)
    out = BookingPolicyOut(**booking_policy.current(db).as_dict())
    if row is not None:
        out.updated_at, out.updated_by = row.updated_at, row.updated_by
    return out


@router.get("", response_model=BookingPolicyOut)
def get_policy(db: Session = Depends(get_db), _admin: AdminUser = Depends(require_permission("bookings.read"))):
    return _out(db)


@router.patch("", response_model=BookingPolicyOut)
def update_policy(
    body: BookingPolicyPatch, request: Request, db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_permission("settings.write")),
):
    try:
        booking_policy.update(db, admin_id=admin.id, changes=body.changes(), reason=body.reason, request=request)
    except booking_policy.PolicyInvalid as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _out(db)
