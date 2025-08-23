from sqlalchemy.orm import Session
from app.db.models.patient_vital import PatientVital
from app.schemas.patient_vital import PatientVitalCreate, PatientVitalUpdate
from uuid import UUID
from typing import List, Optional
from datetime import datetime
import csv
import io

class PatientVitalService:
    def __init__(self, db: Session):
        self.db = db

    def create_vital(self, data: PatientVitalCreate) -> PatientVital:
        vital = PatientVital(**data.dict())
        self.db.add(vital)
        self.db.commit()
        self.db.refresh(vital)
        return vital

    def get_vitals(self, patient_id: UUID, start: Optional[datetime] = None, end: Optional[datetime] = None) -> List[PatientVital]:
        query = self.db.query(PatientVital).filter(PatientVital.patient_id == patient_id)
        if start:
            query = query.filter(PatientVital.recorded_at >= start)
        if end:
            query = query.filter(PatientVital.recorded_at <= end)
        return query.order_by(PatientVital.recorded_at.desc()).all()

    def get_vital(self, vital_id: UUID) -> Optional[PatientVital]:
        return self.db.query(PatientVital).filter(PatientVital.id == vital_id).first()

    def update_vital(self, vital_id: UUID, data: PatientVitalUpdate) -> Optional[PatientVital]:
        vital = self.get_vital(vital_id)
        if not vital:
            return None
        for key, value in data.dict(exclude_unset=True).items():
            setattr(vital, key, value)
        self.db.commit()
        self.db.refresh(vital)
        return vital

    def delete_vital(self, vital_id: UUID) -> bool:
        vital = self.get_vital(vital_id)
        if not vital:
            return False
        self.db.delete(vital)
        self.db.commit()
        return True

    def download_vitals_csv(self, patient_id: UUID) -> str:
        vitals = self.get_vitals(patient_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "patient_id", "recorded_by_user_id", "source", "vital_type", "value", "unit", "recorded_at", "notes"])
        for v in vitals:
            writer.writerow([
                str(v.id), str(v.patient_id), str(v.recorded_by_user_id), v.source.value, v.vital_type, v.value, v.unit, v.recorded_at.isoformat(), v.notes or ""
            ])
        return output.getvalue() 