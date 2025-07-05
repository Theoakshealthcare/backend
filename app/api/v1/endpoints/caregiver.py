from datetime import timedelta
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.models.user import User
from app.schemas.caregiver import (
    CaregiverProfile,
    CaregiverProfileStep1,
    CaregiverProfileStep2,
    CaregiverConsentData,
    DocumentUploadResponse,
    IdentityVerificationResponse,
    SpecializationOptions,
    VerificationResultRequest,
)
from app.services.caregiver import CaregiverService

router = APIRouter()


def get_caregiver_service(db: Session = Depends(get_db)) -> CaregiverService:
    return CaregiverService(db)


@router.get("/profile", response_model=CaregiverProfile)
def get_caregiver_profile(
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Get caregiver profile"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    caregiver = caregiver_service.get_caregiver_by_user_id(current_user.id)
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    return caregiver


@router.put("/profile/step1", response_model=CaregiverProfile)
def update_profile_step1(
    profile_data: CaregiverProfileStep1,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Update caregiver profile - Step 1 (Personal Information)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        caregiver = caregiver_service.update_profile_step1(current_user.id, profile_data)
        return caregiver
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.put("/profile/step2", response_model=CaregiverProfile)
def update_profile_step2(
    profile_data: CaregiverProfileStep2,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Update caregiver profile - Step 2 (Professional Information)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        caregiver = caregiver_service.update_profile_step2(current_user.id, profile_data)
        return caregiver
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/documents/upload", response_model=DocumentUploadResponse)
def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(...),
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Upload caregiver document (license, certification, etc.)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    # Validate file type
    allowed_types = ["application/pdf", "image/jpeg", "image/png", "image/jpg"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Only PDF and image files are allowed."
        )
    
    # Validate file size (10MB max)
    if file.size > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size too large. Maximum 10MB allowed."
        )
    
    try:
        document = caregiver_service.upload_document(current_user.id, file, document_type)
        return document
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.put("/consent", response_model=CaregiverProfile)
def update_consent(
    consent_data: CaregiverConsentData,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Update caregiver consent preferences"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        caregiver = caregiver_service.update_consent(current_user.id, consent_data)
        return caregiver
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/verification-status", response_model=dict)
def get_verification_status(
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Get current verification status"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        status = caregiver_service.get_verification_status(current_user.id)
        return status
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/form-options", response_model=dict)
def get_form_options() -> Any:
    """Get form options for dropdowns (specializations, service areas, etc.)"""
    return CaregiverService.get_form_options()


@router.post("/identity-verification/result", response_model=dict)
def receive_verification_result(
    payload: VerificationResultRequest,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Receive and store verification result from mobile app (Prembly)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    try:
        caregiver_service.process_verification_result(current_user.id, payload.type, payload.result)
        return {"message": "Verification result received and stored."}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
