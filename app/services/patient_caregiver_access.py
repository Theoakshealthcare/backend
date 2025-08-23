from sqlalchemy.orm import Session
from app.db.models.patient_caregiver_access import PatientCaregiverAccess
from app.schemas.patient_caregiver_access import PatientCaregiverAccessGrant, PatientCaregiverAccessRevoke
from uuid import UUID
from typing import List, Optional
from datetime import datetime

class PatientCaregiverAccessService:
    def __init__(self, db: Session):
        self.db = db

    def grant_access(self, data: PatientCaregiverAccessGrant) -> PatientCaregiverAccess:
        # Check if already granted and not revoked
        access = self.db.query(PatientCaregiverAccess).filter(
            PatientCaregiverAccess.patient_id == data.patient_id,
            PatientCaregiverAccess.caregiver_id == data.caregiver_id,
            PatientCaregiverAccess.revoked_at == None
        ).first()
        if access:
            return access
        access = PatientCaregiverAccess(
            patient_id=data.patient_id,
            caregiver_id=data.caregiver_id,
            granted_by_patient=True,
            granted_at=datetime.utcnow(),
            revoked_at=None
        )
        self.db.add(access)
        self.db.commit()
        self.db.refresh(access)
        return access

    def revoke_access(self, data: PatientCaregiverAccessRevoke) -> Optional[PatientCaregiverAccess]:
        access = self.db.query(PatientCaregiverAccess).filter(
            PatientCaregiverAccess.patient_id == data.patient_id,
            PatientCaregiverAccess.caregiver_id == data.caregiver_id,
            PatientCaregiverAccess.revoked_at == None
        ).first()
        if not access:
            return None
        access.revoked_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(access)
        return access

    def list_access(self, patient_id: UUID) -> List[PatientCaregiverAccess]:
        return self.db.query(PatientCaregiverAccess).filter(
            PatientCaregiverAccess.patient_id == patient_id,
            PatientCaregiverAccess.revoked_at == None
        ).all()

    def has_access(self, patient_id: UUID, caregiver_id: UUID) -> bool:
        access = self.db.query(PatientCaregiverAccess).filter(
            PatientCaregiverAccess.patient_id == patient_id,
            PatientCaregiverAccess.caregiver_id == caregiver_id,
            PatientCaregiverAccess.revoked_at == None
        ).first()
        return access is not None 