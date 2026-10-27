"""The client profile for a caregiver's live visit. Mounted under /caregiver."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_caregiver, get_db
from app.db.models.caregiver import Caregiver
from app.db.models.user import User
from app.schemas.caregiver_client import CaregiverClientProfile
from app.services.caregiver_clients import ClientNotAvailable, build_client_profile

router = APIRouter()

NOT_FOUND = "Client not found"


@router.get(
    "/bookings/{booking_id}/client",
    response_model=CaregiverClientProfile,
    responses={404: {"description": "The booking is not yours, is not accepted, on the way or in progress, "
                                    "or does not exist. One answer for all of these."}},
)
def get_client_profile(
    booking_id: UUID,
    response: Response,
    current_user: User = Depends(get_current_caregiver),
    db: Session = Depends(get_db),
) -> Any:
    """Who the visit is for: name, age, sex, emergency contact, medical history if stored, and the latest
    vitals when the vitals rule allows them. Health data: never cached."""
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if caregiver is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    try:
        profile = build_client_profile(db, caregiver, booking_id)
    except ClientNotAvailable:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    response.headers["Cache-Control"] = "no-store"
    return profile
