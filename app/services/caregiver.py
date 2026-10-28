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


NIGERIAN_STATES = [
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue", "Borno",
    "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "Gombe", "Imo", "Jigawa",
    "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi", "Kwara", "Lagos", "Nasarawa", "Niger",
    "Ogun", "Ondo", "Osun", "Oyo", "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe",
    "Zamfara", "Federal Capital Territory",
]


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
        # FOR UPDATE on the caregiver row: serialises with an admin's approve, which checks the
        # licence documents under the same lock (so a new pending licence can't slip in mid-approve).
        caregiver = (
            self.db.query(Caregiver)
            .filter(Caregiver.user_id == user_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
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
        
        # Submitting consent only starts (or restarts) a review. It must never overwrite an
        # admin decision: an approved, suspended or rejected caregiver stays as they are, and a
        # background check that has been decided is not reset.
        if all([consent_data.terms_accepted, consent_data.privacy_policy_accepted,
                consent_data.background_check_consent]):
            if caregiver.profile_status in (None, "incomplete", "changes_requested"):
                caregiver.profile_status = "under_review"
            if caregiver.background_check_status in (None, "pending"):
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
            "profile_status": caregiver.profile_status,
            "rejection_reason": caregiver.rejection_reason,
            "changes_requested": caregiver.changes_requested,
        }

        return status

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
            "states": NIGERIAN_STATES
        }
