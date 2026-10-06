from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from app.db.models.booking_settings import BookingSettings
from app.schemas.booking_settings import (
    BookingSettingsCreate, BookingSettingsUpdate, BookingSettingsResponse
)
from app.api.deps import get_db, get_current_user, require_permission

router = APIRouter()

# Public GET (all users)
@router.get("/", response_model=List[BookingSettingsResponse])
def get_booking_settings(db: Session = Depends(get_db)):
    settings = db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).all()
    return settings

@router.get("/latest", response_model=BookingSettingsResponse)
def get_latest_booking_settings(db: Session = Depends(get_db)):
    settings = db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Booking settings not found")
    return settings

# Admin-only endpoints
@router.post("/", response_model=BookingSettingsResponse)
def create_booking_settings(
    settings_in: BookingSettingsCreate,
    db: Session = Depends(get_db),
    current_admin=Depends(require_permission("pricing.write"))
):
    settings = BookingSettings(**settings_in.dict())
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings

@router.put("/{settings_id}", response_model=BookingSettingsResponse)
def update_booking_settings(
    settings_id: str,
    settings_in: BookingSettingsUpdate,
    db: Session = Depends(get_db),
    current_admin=Depends(require_permission("pricing.write"))
):
    settings = db.query(BookingSettings).filter(BookingSettings.id == settings_id).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Booking settings not found")
    for key, value in settings_in.dict(exclude_unset=True).items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings

@router.delete("/{settings_id}", status_code=204)
def delete_booking_settings(
    settings_id: str,
    db: Session = Depends(get_db),
    current_admin=Depends(require_permission("pricing.write"))
):
    settings = db.query(BookingSettings).filter(BookingSettings.id == settings_id).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Booking settings not found")
    db.delete(settings)
    db.commit()
    return None 