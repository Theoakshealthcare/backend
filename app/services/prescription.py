from sqlalchemy.orm import Session
from app.db.models.prescription import Prescription, PrescriptionStatus
from app.schemas.prescription import PrescriptionCreate, PrescriptionUpdate
from uuid import UUID
from typing import Optional, List
from app.db.models.prescription_settings import PrescriptionSettings
from app.services.pricing import compute_price

class PrescriptionService:
    def __init__(self, db: Session):
        self.db = db

    def create_prescription(self, data: PrescriptionCreate) -> Prescription:
        # Pricing logic
        settings = self.db.query(PrescriptionSettings).order_by(PrescriptionSettings.created_at.desc()).first()
        if not settings:
            raise Exception("Prescription settings not configured.")
        multiplier = 1.0
        if settings.multipliers and data.delivery_option in settings.multipliers:
            multiplier = settings.multipliers[data.delivery_option]
        price = compute_price(
            base_fee_kobo=settings.base_fee_kobo,
            multiplier=multiplier,
            surge_multiplier=settings.surge_multiplier if settings.surge_pricing else None,
            discount_percent=settings.discount,
        )
        prescription = Prescription(
            **data.dict(exclude={'total_cost'}), total_cost_kobo=price.total_kobo
        )
        self.db.add(prescription)
        self.db.commit()
        self.db.refresh(prescription)
        return prescription

    def get_prescription(self, prescription_id: UUID) -> Optional[Prescription]:
        return self.db.query(Prescription).filter(Prescription.id == prescription_id).first()

    def get_patient_prescriptions(self, patient_id: UUID) -> List[Prescription]:
        return self.db.query(Prescription).filter(Prescription.patient_id == patient_id).order_by(Prescription.created_at.desc()).all()

    def update_prescription(self, prescription_id: UUID, data: PrescriptionUpdate) -> Optional[Prescription]:
        prescription = self.get_prescription(prescription_id)
        if not prescription:
            return None
        for field, value in data.dict(exclude_unset=True).items():
            setattr(prescription, field, value)
        self.db.commit()
        self.db.refresh(prescription)
        return prescription 