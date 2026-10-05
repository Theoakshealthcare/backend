"""Endpoints the caregiver mobile app needs that the onboarding/wallet router doesn't have:
the availability switch and the home summary. Mounted under /caregiver."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_caregiver, get_db
from app.db.models.caregiver import Caregiver
from app.db.models.user import User
from app.schemas.caregiver_app import AvailabilityRequest, AvailabilityResponse, CaregiverHome
from app.services.caregiver_home import LocationRequired, NotApproved, build_home, set_availability

router = APIRouter()


def _caregiver_for(db: Session, user: User) -> Caregiver:
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == user.id).first()
    if not caregiver:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caregiver profile not found")
    return caregiver


@router.post("/availability", response_model=AvailabilityResponse)
def update_availability(
    body: AvailabilityRequest,
    current_user: User = Depends(get_current_caregiver),
    db: Session = Depends(get_db),
) -> Any:
    """Go available (receive new visit offers) or unavailable (stop receiving them).

    Accepted bookings and offers already sent are unaffected. Going available needs a location on
    file (409 otherwise); the position must also stay fresh (see LOCATION_FRESHNESS_MINUTES) for
    offers to arrive. Every location ping carries its own `is_available` (default true), so the app
    must send the current value on each ping or a ping will switch the caregiver back on.
    """
    caregiver = _caregiver_for(db, current_user)
    try:
        return AvailabilityResponse(is_available=set_availability(db, caregiver, body.is_available))
    except NotApproved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Your account is not approved to change availability")
    except LocationRequired:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Share your location before going available")


@router.get("/home", response_model=CaregiverHome)
def get_home(
    current_user: User = Depends(get_current_caregiver),
    db: Session = Depends(get_db),
) -> Any:
    """Everything the home screen shows, in one call."""
    return build_home(db, _caregiver_for(db, current_user))
