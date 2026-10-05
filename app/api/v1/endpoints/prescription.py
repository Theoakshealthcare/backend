from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query, Body
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID
import os

from app.api.deps import get_db, get_current_user, get_current_admin, user_is_admin
from app.db.models.user import User
from app.db.models.patient import Patient
from app.schemas.prescription import PrescriptionCreate, PrescriptionResponse, PrescriptionUpdate
from app.services.prescription import PrescriptionService
from app.db.models.prescription_settings import PrescriptionSettings
from app.schemas.prescription_settings import (
    PrescriptionSettingsCreate, PrescriptionSettingsUpdate, PrescriptionSettingsResponse
)

router = APIRouter()

# --- ADMIN SETTINGS ENDPOINTS (must come first) ---
@router.get("/settings", response_model=List[PrescriptionSettingsResponse])
def get_prescription_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = db.query(PrescriptionSettings).order_by(PrescriptionSettings.created_at.desc()).all()
    return settings

@router.get("/settings/latest", response_model=PrescriptionSettingsResponse)
def get_latest_prescription_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = db.query(PrescriptionSettings).order_by(PrescriptionSettings.created_at.desc()).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Prescription settings not found")
    return settings

@router.post("/settings", response_model=PrescriptionSettingsResponse, status_code=status.HTTP_201_CREATED)
def create_prescription_settings(
    settings_in: PrescriptionSettingsCreate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin)
):
    settings = PrescriptionSettings(**settings_in.dict())
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings

@router.put("/settings/{settings_id}", response_model=PrescriptionSettingsResponse)
def update_prescription_settings(
    settings_id: str,
    settings_in: PrescriptionSettingsUpdate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin)
):
    settings = db.query(PrescriptionSettings).filter(PrescriptionSettings.id == settings_id).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Prescription settings not found")
    for key, value in settings_in.dict(exclude_unset=True).items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings

@router.delete("/settings/{settings_id}", status_code=204)
def delete_prescription_settings(
    settings_id: str,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin)
):
    settings = db.query(PrescriptionSettings).filter(PrescriptionSettings.id == settings_id).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Prescription settings not found")
    db.delete(settings)
    db.commit()
    return None

# --- END ADMIN SETTINGS ENDPOINTS ---

UPLOAD_DIR = "uploaded_notes"
os.makedirs(UPLOAD_DIR, exist_ok=True)

def get_prescription_service(db: Session = Depends(get_db)) -> PrescriptionService:
    return PrescriptionService(db)


def _require_patient_access(db: Session, user: User, patient_id: UUID) -> None:
    """Allow the patient who owns `patient_id`, or an admin. Everyone else gets 403."""
    if user_is_admin(db, user):
        return
    if user.user_type == "patient":
        patient = db.query(Patient).filter(Patient.user_id == user.id).first()
        if patient and patient.id == patient_id:
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


def _get_accessible_prescription(
    prescription_id: UUID, db: Session, user: User, service: PrescriptionService
):
    prescription = service.get_prescription(prescription_id)
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    _require_patient_access(db, user, prescription.patient_id)
    return prescription

@router.post("/create", response_model=PrescriptionResponse)
def create_prescription(
    prescription_data: PrescriptionCreate,
    current_user: User = Depends(get_current_user),
    prescription_service: PrescriptionService = Depends(get_prescription_service),
    db: Session = Depends(get_db),
):
    # Ensure patient exists
    patient = db.query(Patient).filter(Patient.id == prescription_data.patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    _require_patient_access(db, current_user, patient.id)
    # never trust the client-supplied requester; it is always the authenticated caller
    prescription_data = prescription_data.copy(update={"requested_by_user_id": current_user.id})
    prescription = prescription_service.create_prescription(prescription_data)
    return prescription

@router.get("/patient/{patient_id}", response_model=List[PrescriptionResponse])
def get_patient_prescriptions(
    patient_id: UUID,
    prescription_service: PrescriptionService = Depends(get_prescription_service),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_patient_access(db, current_user, patient_id)
    return prescription_service.get_patient_prescriptions(patient_id)

@router.get("/{prescription_id}", response_model=PrescriptionResponse)
def get_prescription(
    prescription_id: UUID,
    prescription_service: PrescriptionService = Depends(get_prescription_service),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_accessible_prescription(prescription_id, db, current_user, prescription_service)

@router.patch("/{prescription_id}", response_model=PrescriptionResponse)
def update_prescription(
    prescription_id: UUID,
    update_data: PrescriptionUpdate,
    prescription_service: PrescriptionService = Depends(get_prescription_service),
    current_admin: User = Depends(get_current_admin),
):
    prescription = prescription_service.update_prescription(prescription_id, update_data)
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    return prescription

@router.post("/{prescription_id}/upload_note", response_model=PrescriptionResponse)
async def upload_doctor_note(
    prescription_id: UUID,
    file: UploadFile = File(...),
    prescription_service: PrescriptionService = Depends(get_prescription_service),
    current_admin: User = Depends(get_current_admin),
):
    prescription = prescription_service.get_prescription(prescription_id)
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    # Save file
    file_ext = os.path.splitext(file.filename)[1]
    file_name = f"{prescription_id}_doctor_note{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    with open(file_path, "wb") as f:
        f.write(await file.read())
    # Update prescription
    update_data = PrescriptionUpdate(doctor_note_path=file_path)
    updated = prescription_service.update_prescription(prescription_id, update_data)
    return updated

@router.get("/check_availability/{medication_name}")
def check_drug_availability(
    medication_name: str,
    current_user: User = Depends(get_current_user),
):
    # Mock logic: always available
    # In future, integrate with pharmacy inventory
    return {"medication_name": medication_name, "available": True, "estimated_cost": 250000}

@router.patch("/{prescription_id}/status", response_model=PrescriptionResponse)
def update_prescription_status(
    prescription_id: UUID,
    status: str = Body(..., embed=True),
    prescription_service: PrescriptionService = Depends(get_prescription_service),
    current_admin: User = Depends(get_current_admin),
):
    update_data = PrescriptionUpdate(status=status)
    prescription = prescription_service.update_prescription(prescription_id, update_data)
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    return prescription

@router.get("/{prescription_id}/status")
def get_prescription_status(
    prescription_id: UUID,
    prescription_service: PrescriptionService = Depends(get_prescription_service),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    prescription = _get_accessible_prescription(prescription_id, db, current_user, prescription_service)
    return {"status": prescription.status} 