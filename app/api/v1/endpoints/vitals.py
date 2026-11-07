from fastapi import APIRouter, Depends, HTTPException, status, Query, Response, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID
from datetime import datetime
import asyncio

from app.api.deps import get_db, get_current_user
from app.db.models.user import User
from app.db.models.patient import Patient
from app.db.models.caregiver import Caregiver
from app.db.models.patient_vital import PatientVital
from app.schemas.patient_vital import PatientVitalCreate, PatientVitalUpdate, PatientVitalResponse
from app.services.patient_vital import PatientVitalService
from app.services.vital_rules import VitalRuleError, check_notes, check_reading
from app.services.gemini import GeminiService, gemini_service
from app.services.patient_caregiver_access import PatientCaregiverAccessService
from app.services.vitals_access import caregiver_can_access_vitals
from app.schemas.patient_caregiver_access import (
    PatientCaregiverAccessGrant, PatientCaregiverAccessRevoke, PatientCaregiverAccessResponse
)

router = APIRouter()

def get_vital_service(db: Session = Depends(get_db)):
    return PatientVitalService(db)

def get_gemini_service():
    return gemini_service

def get_access_service(db: Session = Depends(get_db)):
    return PatientCaregiverAccessService(db)

# --- Helper: Permission check ---
def can_access_vitals(current_user: User, patient_id: UUID, db: Session) -> bool:
    if current_user.user_type == "patient":
        patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
        return patient and str(patient.id) == str(patient_id)
    elif current_user.user_type == "caregiver":
        caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
        if not caregiver:
            return False
        return caregiver_can_access_vitals(db, caregiver.id, patient_id)
    return False

# --- Create Vital ---
@router.post("/", response_model=PatientVitalResponse)
def create_vital(
    vital_data: PatientVitalCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientVitalService = Depends(get_vital_service),
):
    if not can_access_vitals(current_user, vital_data.patient_id, db):
        raise HTTPException(status_code=403, detail="Access denied")
    return service.create_vital(vital_data)

# --- List Vitals ---
@router.get("/{patient_id}", response_model=List[PatientVitalResponse])
def list_vitals(
    patient_id: UUID,
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientVitalService = Depends(get_vital_service),
):
    if not can_access_vitals(current_user, patient_id, db):
        raise HTTPException(status_code=403, detail="Access denied")
    return service.get_vitals(patient_id, start, end)

# --- Update Vital ---
@router.put("/{vital_id}", response_model=PatientVitalResponse)
def update_vital(
    vital_id: UUID,
    update_data: PatientVitalUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientVitalService = Depends(get_vital_service),
):
    vital = service.get_vital(vital_id)
    if not vital:
        raise HTTPException(status_code=404, detail="Vital not found")
    if not can_access_vitals(current_user, vital.patient_id, db):
        raise HTTPException(status_code=403, detail="Access denied")
    changes = update_data.model_dump(exclude_unset=True)
    try:
        if {"vital_type", "value", "unit"} & changes.keys():  # an edit of notes alone never re-judges an old row
            clean = check_reading(changes.get("vital_type", vital.vital_type), changes.get("value", vital.value),
                                  changes.get("unit", vital.unit))
            for key, cleaned in zip(("vital_type", "value", "unit"), clean):
                if key in changes:
                    setattr(update_data, key, cleaned)
        check_notes(changes.get("notes"))
    except VitalRuleError as exc:
        raise HTTPException(status_code=422, detail=[{"type": "vital_invalid", "loc": ["body"], "msg": str(exc)}])
    return service.update_vital(vital_id, update_data)

# --- Delete Vital ---
@router.delete("/{vital_id}", response_model=dict)
def delete_vital(
    vital_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientVitalService = Depends(get_vital_service),
):
    vital = service.get_vital(vital_id)
    if not vital:
        raise HTTPException(status_code=404, detail="Vital not found")
    if not can_access_vitals(current_user, vital.patient_id, db):
        raise HTTPException(status_code=403, detail="Access denied")
    service.delete_vital(vital_id)
    return {"ok": True}

# --- Download Vitals as CSV ---
@router.get("/{patient_id}/download")
def download_vitals(
    patient_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientVitalService = Depends(get_vital_service),
):
    if not can_access_vitals(current_user, patient_id, db):
        raise HTTPException(status_code=403, detail="Access denied")
    csv_data = service.download_vitals_csv(patient_id)
    return Response(content=csv_data, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=vitals_{patient_id}.csv"})

# --- AI Insights ---
@router.get("/{patient_id}/insights", response_model=dict)
async def get_vitals_insights(
    patient_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientVitalService = Depends(get_vital_service),
    gemini: GeminiService = Depends(get_gemini_service),
):
    if not can_access_vitals(current_user, patient_id, db):
        raise HTTPException(status_code=403, detail="Access denied")
    vitals = service.get_vitals(patient_id)
    vitals_data = [
        {
            "type": v.vital_type,
            "value": v.value,
            "unit": v.unit,
            "recorded_at": v.recorded_at.isoformat(),
            "notes": v.notes,
        }
        for v in vitals
    ]
    # Call Gemini for insights (async)
    insights = await gemini.analyze_vitals(vitals_data)
    return insights

# --- Caregiver access grants -----------------------------------------------------------------------------
# Ids: `patient_id` is the PATIENT's id (patients.id, `/auth/me` -> patient.id) and `caregiver_id` the caregiver's
# (caregivers.id), never the user ids. The patient is always the signed-in user's own profile: nobody grants,
# revokes or lists on behalf of someone else.

def _own_patient(db: Session, user: User) -> Optional[Patient]:
    return db.query(Patient).filter(Patient.user_id == user.id).first()


def _own_patient_or_403(db: Session, user: User, patient_id: UUID, what: str) -> Patient:
    if user.user_type != "patient":
        raise HTTPException(status_code=403, detail=f"Only the patient can {what} access.")
    patient = _own_patient(db, user)
    if patient is None or patient.id != patient_id:
        raise HTTPException(status_code=403, detail="You can only manage access to your own vitals.")
    return patient


@router.post("/access/grant", response_model=PatientCaregiverAccessResponse)
def grant_caregiver_access(
    grant_data: PatientCaregiverAccessGrant,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientCaregiverAccessService = Depends(get_access_service),
):
    _own_patient_or_403(db, current_user, grant_data.patient_id, "grant")
    caregiver = (
        db.query(Caregiver).join(User, User.id == Caregiver.user_id)
        .filter(Caregiver.id == grant_data.caregiver_id, User.is_active.is_(True)).first()
    )
    if caregiver is None:
        raise HTTPException(status_code=404, detail="Caregiver not found.")
    return service.grant_access(grant_data)


@router.post("/access/revoke", response_model=PatientCaregiverAccessResponse)
def revoke_caregiver_access(
    revoke_data: PatientCaregiverAccessRevoke,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientCaregiverAccessService = Depends(get_access_service),
):
    _own_patient_or_403(db, current_user, revoke_data.patient_id, "revoke")
    access = service.revoke_access(revoke_data)
    if not access:
        raise HTTPException(status_code=404, detail="Access not found or already revoked.")
    return access


@router.get("/access/list", response_model=List[PatientCaregiverAccessResponse])
def list_caregiver_access(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientCaregiverAccessService = Depends(get_access_service),
):
    """A patient: the caregivers they have granted access (with `caregiver_name`). A caregiver: the patients who
    granted them access (with `patient_name`). Active grants only."""
    if current_user.user_type == "patient":
        patient = _own_patient(db, current_user)
        return service.list_access(patient.id) if patient else []
    if current_user.user_type == "caregiver":
        caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
        return service.list_granted_to(caregiver.id) if caregiver else []
    raise HTTPException(status_code=403, detail="Only patients and caregivers can list access.")
