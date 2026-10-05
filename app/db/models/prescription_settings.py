import uuid
from datetime import datetime
from sqlalchemy import BigInteger, Column, DateTime, Float, String, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from app.core.money import kobo_to_naira, to_kobo
from app.db.base import Base

class PrescriptionSettings(Base):
    __tablename__ = "prescription_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    base_fee_kobo = Column(BigInteger, nullable=False, default=500000)  # Base fee per prescription, in kobo
    multipliers = Column(JSON, nullable=True)  # e.g. {"urgent": 1.5, "regular": 1.0}
    surge_pricing = Column(Boolean, default=False)
    surge_multiplier = Column(Float, nullable=True, default=1.0)
    discount = Column(Float, nullable=True, default=0.0)  # Discount in percent
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def base_fee(self):
        """Naira view of base_fee_kobo; the admin API still speaks naira."""
        return kobo_to_naira(self.base_fee_kobo) if self.base_fee_kobo is not None else None

    @base_fee.setter
    def base_fee(self, naira):
        self.base_fee_kobo = to_kobo(naira)
