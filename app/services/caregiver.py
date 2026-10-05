import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from uuid import UUID
from sqlalchemy.orm import Session
from fastapi import UploadFile

from app.db.models.caregiver import Caregiver, CaregiverDocument, CaregiverCertification
from app.schemas.caregiver import (
    CaregiverProfileStep1, 
    CaregiverProfileStep2, 
    CaregiverConsentData
)
from app.core.config import settings


class CaregiverService:
    DOCUMENT_EXTENSIONS = {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
    }

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def resolve_document_path(file_path: str) -> Optional[Path]:
        """Resolve a stored document key to a file inside PRIVATE_UPLOAD_DIR.

        Returns None if the file doesn't exist or the key would escape the private directory
        (absolute path, `..`, symlink), so a bad DB row can never be used to read other files.
        """
        base = Path(settings.PRIVATE_UPLOAD_DIR).resolve()
        candidate = (base / file_path).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            return None
        return candidate

    def get_caregiver_by_user_id(self, user_id: UUID) -> Optional[Caregiver]:
        return self.db.query(Caregiver).filter(Caregiver.user_id == user_id).first()

    def update_profile_step1(self, user_id: UUID, profile_data: CaregiverProfileStep1) -> Caregiver:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver:
            raise ValueError("Caregiver not found")

        # Update fields
        for field, value in profile_data.dict(exclude_unset=True).items():
            setattr(caregiver, field, value)
        
        caregiver.profile_completion_step = max(caregiver.profile_completion_step, 1)
        caregiver.updated_at = datetime.utcnow()
        
        self.db.commit()
        self.db.refresh(caregiver)
        return caregiver

    def update_profile_step2(self, user_id: UUID, profile_data: CaregiverProfileStep2) -> Caregiver:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver:
            raise ValueError("Caregiver not found")

        # Update fields
        for field, value in profile_data.dict(exclude_unset=True).items():
            setattr(caregiver, field, value)
        
        caregiver.profile_completion_step = max(caregiver.profile_completion_step, 2)
        caregiver.updated_at = datetime.utcnow()
        
        self.db.commit()
        self.db.refresh(caregiver)
        return caregiver

    def upload_document(self, user_id: UUID, file: UploadFile, document_type: str) -> CaregiverDocument:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver:
            raise ValueError("Caregiver not found")

        # Documents are private: stored under PRIVATE_UPLOAD_DIR (never the public /uploads
        # mount) and only served through GET /caregiver/documents/{id}/file.
        # The extension comes from the validated content type, never from the client filename.
        extension = self.DOCUMENT_EXTENSIONS.get(file.content_type or "", "bin")
        key = f"caregivers/{caregiver.id}/documents/{uuid.uuid4()}.{extension}"
        file_path = Path(settings.PRIVATE_UPLOAD_DIR) / key
        file_path.parent.mkdir(parents=True, exist_ok=True)

        content = file.file.read()
        file_path.write_bytes(content)

        # Create document record
        document = CaregiverDocument(
            caregiver_id=caregiver.id,
            document_type=document_type,
            document_name=file.filename,
            file_path=key,
            file_size=len(content),
            mime_type=file.content_type
        )

        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document

    def update_consent(self, user_id: UUID, consent_data: CaregiverConsentData) -> Caregiver:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver:
            raise ValueError("Caregiver not found")

        caregiver.terms_accepted = consent_data.terms_accepted
        caregiver.privacy_policy_accepted = consent_data.privacy_policy_accepted
        caregiver.background_check_consent = consent_data.background_check_consent
        caregiver.consent_date = datetime.utcnow()
        
        # If all consents are given, update profile status
        if all([consent_data.terms_accepted, consent_data.privacy_policy_accepted, 
                consent_data.background_check_consent]):
            caregiver.profile_status = "under_review"
            caregiver.background_check_status = "in_progress"

        self.db.commit()
        self.db.refresh(caregiver)
        return caregiver

    def get_verification_status(self, user_id: UUID) -> Dict[str, Any]:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver:
            raise ValueError("Caregiver not found")

        status = {
            "profile_completion_step": caregiver.profile_completion_step,
            "identity_verification_status": caregiver.identity_verification_status,
            "background_check_status": caregiver.background_check_status,
            "profile_status": caregiver.profile_status
        }

        return status

    def process_verification_result(self, user_id: UUID, verification_type: str, result: dict) -> None:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver:
            raise ValueError("Caregiver not found")

        # The client is not trusted: the submitted payload is stored as evidence only, and the
        # most the client can do is move the status to "in_progress". "verified" / "failed" are
        # admin decisions (and are never overwritten by a resubmission).
        def next_status(current: Optional[str]) -> str:
            return current if current in ("verified", "failed") else "in_progress"

        if verification_type == "document":
            caregiver.identity_verification_status = next_status(caregiver.identity_verification_status)
            caregiver.document_verification_result = result
        elif verification_type == "face":
            caregiver.face_verification_status = next_status(caregiver.face_verification_status)
            caregiver.face_verification_result = result
        else:
            raise ValueError("Invalid verification type")

        caregiver.profile_completion_step = max(caregiver.profile_completion_step, 3)
        caregiver.updated_at = datetime.utcnow()
        self.db.commit()

    @staticmethod
    def get_form_options() -> Dict[str, List[str]]:
        return {
            "specializations": [
                "Elderly Care",
                "Disability Support",
                "Chronic Illness Care",
                "Post-Surgery Care",
                "Dementia Care",
                "Palliative Care",
                "Mental Health Support",
                "Physical Therapy Assistance",
                "Medication Management",
                "Companionship"
            ],
            "service_areas": [
                "Personal Care",
                "Household Tasks",
                "Transportation",
                "Meal Preparation",
                "Medication Reminders",
                "Exercise Assistance",
                "Social Activities",
                "Medical Appointments",
                "Emergency Response",
                "24/7 Care"
            ],
            "work_hours": [
                "Morning (6 AM - 12 PM)",
                "Afternoon (12 PM - 6 PM)",
                "Evening (6 PM - 12 AM)",
                "Night (12 AM - 6 AM)",
                "Weekdays Only",
                "Weekends Only",
                "Flexible Hours",
                "24/7 Availability"
            ],
            "states": [
                "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
                "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
                "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
                "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
                "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
                "New Hampshire", "New Jersey", "New Mexico", "New York",
                "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
                "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
                "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
                "West Virginia", "Wisconsin", "Wyoming"
            ]
        }
