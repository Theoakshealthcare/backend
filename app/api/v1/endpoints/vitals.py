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
from app.services.gemini import GeminiService
from app.services.patient_caregiver_access import PatientCaregiverAccessService
from app.schemas.patient_caregiver_access import (
    PatientCaregiverAccessGrant, PatientCaregiverAccessRevoke, PatientCaregiverAccessResponse
)

router = APIRouter()

def get_vital_service(db: Session = Depends(get_db)):
    return PatientVitalService(db)

def get_gemini_service():
    return GeminiService()

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
        # Check for active booking
        from app.db.models.booking import Booking
        active = db.query(Booking).filter(
            Booking.patient_id == patient_id,
            Booking.caregiver_id == caregiver.id,
            Booking.status.in_(["active", "in_progress"])
        ).first()
        # Check for explicit access
        access_service = PatientCaregiverAccessService(db)
        has_explicit = access_service.has_access(patient_id, caregiver.id)
        return bool(active) or has_explicit
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

# --- Grant Caregiver Access ---
@router.post("/access/grant", response_model=PatientCaregiverAccessResponse)
def grant_caregiver_access(
    grant_data: PatientCaregiverAccessGrant,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientCaregiverAccessService = Depends(get_access_service),
):
    # Only patient can grant access to their own caregivers
    if current_user.user_type != "patient" or str(current_user.id) != str(grant_data.patient_id):
        raise HTTPException(status_code=403, detail="Only the patient can grant access.")
    access = service.grant_access(grant_data)
    return access

# --- Revoke Caregiver Access ---
@router.post("/access/revoke", response_model=PatientCaregiverAccessResponse)
def revoke_caregiver_access(
    revoke_data: PatientCaregiverAccessRevoke,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientCaregiverAccessService = Depends(get_access_service),
):
    if current_user.user_type != "patient" or str(current_user.id) != str(revoke_data.patient_id):
        raise HTTPException(status_code=403, detail="Only the patient can revoke access.")
    access = service.revoke_access(revoke_data)
    if not access:
        raise HTTPException(status_code=404, detail="Access not found or already revoked.")
    return access

# --- List Caregiver Access ---
@router.get("/access/list", response_model=List[PatientCaregiverAccessResponse])
def list_caregiver_access(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: PatientCaregiverAccessService = Depends(get_access_service),
):
    if current_user.user_type != "patient":
        raise HTTPException(status_code=403, detail="Only the patient can list access.")
    return service.list_access(current_user.id) 