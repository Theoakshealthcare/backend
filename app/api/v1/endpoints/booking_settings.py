"""Pricing settings. Reads need an admin with ``bookings.read``; writes need ``pricing.write`` and a reason.

Settings are versioned: POST (the first version only) and PUT both publish a new row and write an ``admin_audit_log`` row; old rows (the ones
quotes and bookings were priced with) are never edited or removed. The app and the WhatsApp flow read pricing straight
from the database, not through these routes.
"""

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.db.models.admin import AdminUser
from app.db.models.booking_settings import BookingSettings
from app.schemas.booking_settings import BookingSettingsCreate, BookingSettingsResponse, BookingSettingsUpdate
from app.services import booking_settings as service

router = APIRouter()


@router.get("/", response_model=List[BookingSettingsResponse])
def get_booking_settings(
    db: Session = Depends(get_db),
    _admin: AdminUser = Depends(require_permission("bookings.read")),
):
    return db.query(BookingSettings).order_by(BookingSettings.created_at.desc(), BookingSettings.id.desc()).all()


@router.get("/latest", response_model=BookingSettingsResponse)
def get_latest_booking_settings(
    db: Session = Depends(get_db),
    _admin: AdminUser = Depends(require_permission("bookings.read")),
):
    settings = service.latest(db)
    if not settings:
        raise HTTPException(status_code=404, detail="Booking settings not found")
    return settings


@router.post("/", response_model=BookingSettingsResponse)
def create_booking_settings(
    settings_in: BookingSettingsCreate, request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_permission("pricing.write")),
):
    """Create the very first pricing version. Once one exists this is a 409: edit the latest with PUT."""
    try:
        return service.publish(db, admin_id=admin.id, body=settings_in, request=request)
    except service.SettingsExist as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Pricing already exists; use PUT on the latest version ({exc.latest_id})")


@router.put("/{settings_id}", response_model=BookingSettingsResponse)
def update_booking_settings(
    settings_id: UUID, settings_in: BookingSettingsUpdate, request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_permission("pricing.write")),
):
    """Publish a new version that replaces ``settings_id``, which must still be the latest. The old row is untouched."""
    try:
        return service.publish(db, admin_id=admin.id, body=settings_in, supersedes=settings_id, request=request)
    except service.SettingsNotFound:
        db.rollback()
        raise HTTPException(status_code=404, detail="Booking settings not found")
    except service.SettingsSuperseded as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A newer pricing version exists ({exc.latest_id}); reload it and edit that one")


@router.delete("/{settings_id}", responses={409: {"description": "Always: pricing versions are never deleted"}})
def delete_booking_settings(
    settings_id: UUID,
    _admin: AdminUser = Depends(require_permission("pricing.write")),
):
    """Refused: versions are the pricing history that quotes and bookings point at. To change the price, publish a new
    version (PUT); to go back, publish the old values again."""
    raise HTTPException(
        status_code=409, detail="Pricing versions cannot be deleted; publish a new version instead")
