import uuid
from datetime import datetime
from sqlalchemy import Column, DateTime, String, Float, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.db.base import Base
import enum
import os
from cryptography.fernet import Fernet, InvalidToken

FERNET_KEY = os.environ.get("VITALS_ENCRYPTION_KEY")
if not FERNET_KEY:
    raise RuntimeError("VITALS_ENCRYPTION_KEY environment variable not set.")
fernet = Fernet(FERNET_KEY)

class VitalSource(enum.Enum):
    manual = "manual"
    wearable = "wearable"

class PatientVital(Base):
    __tablename__ = "patient_vitals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    recorded_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    source = Column(Enum(VitalSource), nullable=False, default=VitalSource.manual)
    vital_type = Column(String, nullable=False)  # e.g., blood_pressure, heart_rate, temperature
    _value = Column("value", String, nullable=False)
    unit = Column(String, nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow)
    _notes = Column("notes", String, nullable=True)

    patient = relationship("Patient", backref="vitals")
    recorder = relationship("User")

    @property
    def value(self):
        try:
            return fernet.decrypt(self._value.encode()).decode()
        except Exception:
            return self._value  # fallback for legacy/plaintext

    @value.setter
    def value(self, val):
        self._value = fernet.encrypt(val.encode()).decode()

    @property
    def notes(self):
        if self._notes is None:
            return None
        try:
            return fernet.decrypt(self._notes.encode()).decode()
        except Exception:
            return self._notes

    @notes.setter
    def notes(self, val):
        if val is None:
            self._notes = None
        else:
            self._notes = fernet.encrypt(val.encode()).decode() 