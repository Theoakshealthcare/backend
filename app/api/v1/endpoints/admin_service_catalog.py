"""Admin: the service catalogue (``/admin/service-catalog``).

Reading needs ``bookings.read`` (same as the pricing and policy reads); changing needs ``pricing.write`` (the permission
that guards the multipliers a service needs before it can be switched on) and writes an ``admin_audit_log`` row. The
catalogue holds no prices: ``priced`` says whether the newest pricing has a multiplier for the service.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.db.models.admin import AdminUser
from app.db.models.service_catalog import ServiceCatalog
from app.schemas.service_catalog import ServiceCatalogOut, ServiceCatalogWrite
from app.services import service_catalog as catalog
from app.services.pricing import is_priced

router = APIRouter()


def _out(row: ServiceCatalog, settings) -> ServiceCatalogOut:
    return ServiceCatalogOut(
        service_type=row.service_type, enabled=row.enabled, display_name=row.display_name, mode=row.mode,
        min_minutes=row.min_minutes, step_minutes=row.step_minutes, max_minutes=row.max_minutes,
        sort_order=row.sort_order, priced=is_priced(settings, row.service_type),
        bookable=catalog.is_bookable(row, settings), updated_at=row.updated_at, updated_by=row.updated_by)


@router.get("", response_model=List[ServiceCatalogOut])
def list_services(db: Session = Depends(get_db), _admin: AdminUser = Depends(require_permission("bookings.read"))):
    settings = catalog.latest_settings(db)
    return [_out(row, settings) for row in catalog.all_rows(db)]


@router.put(
    "/{service_type}", response_model=ServiceCatalogOut,
    responses={
        404: {"description": "Unknown service type"},
        422: {"description": "Invalid body, or detail.code service_has_no_price: set its multiplier in Pricing first"},
    })
def update_service(
    service_type: str, body: ServiceCatalogWrite, request: Request, db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_permission("pricing.write")),
):
    """Replace one service's settings. Idempotent: repeating the same body is a no-op (no second audit row)."""
    try:
        row = catalog.update(db, admin_id=admin.id, service_type=service_type, changes=body.changes(),
                             reason=body.reason, request=request)
    except catalog.ServiceNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Service not found")
    except catalog.ServiceHasNoPrice:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
            "code": "service_has_no_price",
            "message": "This service has no price yet. Set its multiplier in Pricing first, then enable it."})
    return _out(row, catalog.latest_settings(db))
