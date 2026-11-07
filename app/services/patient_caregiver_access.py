from sqlalchemy.orm import Session, joinedload
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
        """Ends EVERY active grant for the pair (a race could have left two), so a revoke always cuts access."""
        rows = self.db.query(PatientCaregiverAccess).filter(
            PatientCaregiverAccess.patient_id == data.patient_id,
            PatientCaregiverAccess.caregiver_id == data.caregiver_id,
            PatientCaregiverAccess.revoked_at == None
        ).all()
        if not rows:
            return None
        now = datetime.utcnow()
        for row in rows:
            row.revoked_at = now
        self.db.commit()
        self.db.refresh(rows[0])
        return rows[0]

    def list_access(self, patient_id: UUID) -> List[PatientCaregiverAccess]:
        """Active grants a patient (``patients.id``) has given, oldest first."""
        return self._active().filter(PatientCaregiverAccess.patient_id == patient_id).all()

    def list_granted_to(self, caregiver_id: UUID) -> List[PatientCaregiverAccess]:
        """Active grants a caregiver (``caregivers.id``) holds: the patients who let them see their vitals."""
        return self._active().filter(PatientCaregiverAccess.caregiver_id == caregiver_id).all()

    def _active(self):
        return (
            self.db.query(PatientCaregiverAccess)
            .options(joinedload(PatientCaregiverAccess.caregiver), joinedload(PatientCaregiverAccess.patient))
            .filter(PatientCaregiverAccess.revoked_at == None)
            .order_by(PatientCaregiverAccess.granted_at)
        )

    def has_access(self, patient_id: UUID, caregiver_id: UUID) -> bool:
        access = self.db.query(PatientCaregiverAccess).filter(
            PatientCaregiverAccess.patient_id == patient_id,
            PatientCaregiverAccess.caregiver_id == caregiver_id,
            PatientCaregiverAccess.revoked_at == None
        ).first()
        return access is not None 