from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID
import json

from app.api.deps import get_db, get_current_user, get_current_admin, get_current_patient
from app.db.models.fallguard import FallGuardQuestion
from app.db.models.patient import Patient
from app.schemas.fallguard import (
    FallGuardQuestionCreate, FallGuardQuestionUpdate, FallGuardQuestionResponse,
    FallGuardAssessmentCreate, FallGuardAssessmentResponse
)
from app.services.fallguard import FallGuardService

router = APIRouter()

def get_fallguard_service(db: Session = Depends(get_db)) -> FallGuardService:
    return FallGuardService(db)

# Patient: Get questions
@router.get("/questions", response_model=List[FallGuardQuestionResponse])
def get_questions(
    current_user=Depends(get_current_patient),
    service: FallGuardService = Depends(get_fallguard_service)
):
    questions = service.get_questions(active_only=True)
    # Parse options JSON for response
    for q in questions:
        q.options = json.loads(q.options)
    return questions

# Patient: Submit assessment
@router.post("/submit", response_model=FallGuardAssessmentResponse)
async def submit_assessment(
    data: FallGuardAssessmentCreate,
    current_user=Depends(get_current_patient),
    service: FallGuardService = Depends(get_fallguard_service)
):
    # Get patient
    db_patient = service.db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not db_patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    assessment = await service.submit_assessment(db_patient.id, data)
    # Prepare answers for response
    answers = []
    for ans in assessment.answers:
        answers.append({
            "id": ans.id,
            "question_id": ans.question_id,
            "answer": ans.answer,
            "score": ans.score
        })
    return FallGuardAssessmentResponse(
        id=assessment.id,
        patient_id=assessment.patient_id,
        total_score=assessment.total_score,
        ai_summary=assessment.ai_summary,
        ai_recommendations=assessment.ai_recommendations,
        created_at=assessment.created_at,
        answers=answers
    )

# Patient: Get assessment history
@router.get("/history", response_model=List[FallGuardAssessmentResponse])
def get_history(
    current_user=Depends(get_current_patient),
    service: FallGuardService = Depends(get_fallguard_service)
):
    db_patient = service.db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not db_patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    assessments = service.get_assessment_history(db_patient.id)
    result = []
    for assessment in assessments:
        answers = []
        for ans in assessment.answers:
            answers.append({
                "id": ans.id,
                "question_id": ans.question_id,
                "answer": ans.answer,
                "score": ans.score
            })
        result.append(FallGuardAssessmentResponse(
            id=assessment.id,
            patient_id=assessment.patient_id,
            total_score=assessment.total_score,
            ai_summary=assessment.ai_summary,
            ai_recommendations=assessment.ai_recommendations,
            created_at=assessment.created_at,
            answers=answers
        ))
    return result

# Admin: CRUD for questions
@router.post("/admin/question", response_model=FallGuardQuestionResponse)
def admin_create_question(
    data: FallGuardQuestionCreate,
    current_user=Depends(get_current_admin),
    service: FallGuardService = Depends(get_fallguard_service)
):
    question = service.create_question(data.dict())
    question.options = json.loads(question.options)
    return question

@router.put("/admin/question/{question_id}", response_model=FallGuardQuestionResponse)
def admin_update_question(
    question_id: UUID,
    data: FallGuardQuestionUpdate,
    current_user=Depends(get_current_admin),
    service: FallGuardService = Depends(get_fallguard_service)
):
    question = service.update_question(question_id, data.dict())
    question.options = json.loads(question.options)
    return question

@router.delete("/admin/question/{question_id}")
def admin_delete_question(
    question_id: UUID,
    current_user=Depends(get_current_admin),
    service: FallGuardService = Depends(get_fallguard_service)
):
    service.delete_question(question_id)
    return {"status": True}

@router.get("/admin/questions", response_model=List[FallGuardQuestionResponse])
def admin_get_questions(
    current_user=Depends(get_current_admin),
    service: FallGuardService = Depends(get_fallguard_service)
):
    questions = service.get_questions(active_only=False)
    for q in questions:
        q.options = json.loads(q.options)
    return questions 