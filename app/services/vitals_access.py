"""Whether a caregiver may read or write a patient's vitals.

A caregiver may only while a visit with that patient is in progress, or while the patient has granted
them access (and not revoked it). Accepted, on-the-way, completed and cancelled visits give no access.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.booking import Booking, BookingStatus
from app.services.patient_caregiver_access import PatientCaregiverAccessService


def caregiver_can_access_vitals(db: Session, caregiver_id: UUID, patient_id: UUID) -> bool:
    in_progress = (
        db.query(Booking.id)
        .filter(
            Booking.patient_id == patient_id,
            Booking.caregiver_id == caregiver_id,
            Booking.status == BookingStatus.IN_PROGRESS,
        )
        .first()
    )
    return in_progress is not None or PatientCaregiverAccessService(db).has_access(patient_id, caregiver_id)
