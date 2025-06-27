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
from app.services.onfido import onfido_service
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

    def initiate_identity_verification(self, user_id: UUID) -> Dict[str, Any]:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver:
            raise ValueError("Caregiver not found")

        try:
            # Create Onfido applicant if not exists
            if not caregiver.onfido_applicant_id:
                name_parts = caregiver.full_name.split(' ', 1)
                first_name = name_parts[0]
                last_name = name_parts[1] if len(name_parts) > 1 else ""

                address_data = None
                if caregiver.house_address and caregiver.state:
                    address_data = {
                        "flat_number": "",
                        "building_name": "",
                        "building_number": "",
                        "street": caregiver.house_address,
                        "sub_street": "",
                        "town": "",
                        "state": caregiver.state,
                        "postcode": "",
                        "country": "USA"
                    }

                applicant = onfido_service.create_applicant(
                    first_name=first_name,
                    last_name=last_name,
                    email=caregiver.email,
                    phone=caregiver.phone_number,
                    dob=caregiver.date_of_birth.isoformat() if caregiver.date_of_birth else None,
                    address=address_data
                )

                caregiver.onfido_applicant_id = applicant["id"]
                self.db.commit()

            # Generate SDK token
            sdk_token = onfido_service.generate_sdk_token(caregiver.onfido_applicant_id)

            # Update verification status
            caregiver.identity_verification_status = "in_progress"
            self.db.commit()

            return {
                "applicant_id": caregiver.onfido_applicant_id,
                "sdk_token": sdk_token
            }

        except Exception as e:
            raise Exception(f"Failed to initiate identity verification: {str(e)}")

    def complete_identity_verification(self, user_id: UUID) -> Dict[str, Any]:
        caregiver = self.get_caregiver_by_user_id(user_id)
        if not caregiver or not caregiver.onfido_applicant_id:
            raise ValueError("Caregiver or applicant not found")

        try:
            # Create verification check
            check = onfido_service.create_check(caregiver.onfido_applicant_id)
            
            caregiver.onfido_check_id = check["id"]
            caregiver.profile_completion_step = max(caregiver.profile_completion_step, 3)
            caregiver.updated_at = datetime.utcnow()
            
            self.db.commit()

            return {
                "check_id": check["id"],
                "status": check["status"]
            }

        except Exception as e:
            raise Exception(f"Failed to complete identity verification: {str(e)}")

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

        # Check Onfido status if check exists
        if caregiver.onfido_check_id:
            try:
                check_status = onfido_service.get_check_status(caregiver.onfido_check_id)
                status["onfido_check_status"] = check_status["status"]
                status["onfido_result"] = check_status.get("result")
                
                # Update local status based on Onfido results
                if check_status["status"] == "complete":
                    if check_status.get("result") == "clear":
                        caregiver.identity_verification_status = "verified"
                    else:
                        caregiver.identity_verification_status = "failed"
                    self.db.commit()
                    
            except Exception as e:
                print(f"Error checking Onfido status: {e}")

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
