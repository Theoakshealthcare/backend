"""The client profile a caregiver sees for one of their visits (read-only).

Allowed only for the assigned caregiver of an accepted, on-the-way or in-progress booking. Anything else
raises ``ClientNotAvailable``, which the endpoint answers with one 404, so the response never says whether
a booking or a patient exists. As with starting a visit, a suspended caregiver can see the client only of a
visit already under way.
"""

from datetime import date
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.booking import Booking, BookingStatus
from app.db.models.care import CareRecipient
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient, PatientMedicalRecord
from app.db.models.patient_vital import PatientVital
from app.services.vitals_access import caregiver_can_access_vitals

LIVE_STATUSES = (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS)
MAX_MEDICAL_RECORDS = 50


class ClientNotAvailable(Exception):
    pass


def age_in_years(born: Optional[date], today: Optional[date] = None) -> Optional[int]:
    if born is None:
        return None
    today = today or date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _emergency_contact(patient: Patient, booking: Booking, is_self: bool) -> Optional[Dict[str, Any]]:
    if is_self:
        contact = {"name": patient.emergency_contact_name, "phone": patient.emergency_contact_phone,
                   "relationship": patient.emergency_contact_relationship}
    else:
        # Whoever booked is the family contact; the patient record's own emergency contact is theirs.
        contact = {"name": patient.full_name, "phone": patient.phone_number, "relationship": booking.booking_for}
    return contact if contact["name"] or contact["phone"] else None


def _medical_history(db: Session, patient: Patient) -> Optional[Dict[str, Any]]:
    records = (
        db.query(PatientMedicalRecord)
        .filter(PatientMedicalRecord.patient_id == patient.id)
        .order_by(PatientMedicalRecord.date_recorded.desc().nullslast(), PatientMedicalRecord.created_at.desc())
        .limit(MAX_MEDICAL_RECORDS)
        .all()
    )
    history = {
        "blood_group": patient.blood_group, "allergies": patient.allergies,
        "medical_conditions": patient.medical_conditions, "medications": patient.medications,
        "records": [
            {"record_type": r.record_type, "title": r.title, "description": r.description,
             "severity": r.severity, "date_recorded": r.date_recorded}
            for r in records
        ],
    }
    stored = any(history[k] for k in ("blood_group", "allergies", "medical_conditions", "medications"))
    return history if stored or records else None


def _latest_vitals(db: Session, patient_id: UUID) -> List[Dict[str, Any]]:
    rows = (
        db.query(PatientVital)
        .filter(PatientVital.patient_id == patient_id)
        .distinct(PatientVital.vital_type)
        .order_by(PatientVital.vital_type, PatientVital.recorded_at.desc())
        .all()
    )
    rows.sort(key=lambda v: v.recorded_at, reverse=True)
    return [{"vital_type": v.vital_type, "value": v.value, "unit": v.unit, "recorded_at": v.recorded_at}
            for v in rows]


def build_client_profile(db: Session, caregiver: Caregiver, booking_id: UUID) -> Dict[str, Any]:
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id, Booking.caregiver_id == caregiver.id, Booking.status.in_(LIVE_STATUSES))
        .first()
    )
    if booking is None:
        raise ClientNotAvailable()
    if caregiver.profile_status != "approved" and booking.status != BookingStatus.IN_PROGRESS:
        raise ClientNotAvailable()
    patient = db.get(Patient, booking.patient_id)
    if patient is None or not patient.is_active:  # an erased patient is anonymised and inactive
        raise ClientNotAvailable()

    is_self = booking.booking_for == "myself"
    recipient = None if is_self or booking.care_recipient_id is None else db.get(CareRecipient, booking.care_recipient_id)
    subject = patient if is_self else recipient  # whose name, age and sex these are (None: not known)

    return {
        "booking_id": booking.id,
        "patient_id": patient.id,
        "full_name": subject.full_name if subject is not None else patient.full_name,
        "profile_image_url": (patient.user.profile_image_url if patient.user else None) if is_self else None,
        "booking_for": booking.booking_for,
        "age": age_in_years(subject.date_of_birth) if subject is not None else None,
        "gender": subject.gender if subject is not None else None,
        "emergency_contact": _emergency_contact(patient, booking, is_self),
        "medical_history": _medical_history(db, patient) if is_self else None,
        "latest_vitals": _latest_vitals(db, patient.id) if caregiver_can_access_vitals(db, caregiver.id, patient.id) else None,
    }
