import os
import uuid
from datetime import datetime
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
    def __init__(self, db: Session):
        self.db = db

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

        # Create upload directory
        upload_dir = f"uploads/caregivers/{caregiver.id}/documents"
        os.makedirs(upload_dir, exist_ok=True)

        # Generate unique filename
        file_extension = file.filename.split('.')[-1] if '.' in file.filename else ''
        unique_filename = f"{uuid.uuid4()}.{file_extension}"
        file_path = os.path.join(upload_dir, unique_filename)

        # Save file
        with open(file_path, "wb") as buffer:
            content = file.file.read()
            buffer.write(content)

        # Create document record
        document = CaregiverDocument(
            caregiver_id=caregiver.id,
            document_type=document_type,
            document_name=file.filename,
            file_path=file_path,
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

        # Store the result (could be expanded to store in a separate table if needed)
        if verification_type == "document":
            caregiver.identity_verification_status = result.get("status", "pending")
            caregiver.document_verification_result = result
        elif verification_type == "face":
            caregiver.face_verification_status = result.get("status", "pending")
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
