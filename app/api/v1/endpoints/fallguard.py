from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_patient, get_db, require_permission
from app.db.models.patient import Patient
from app.schemas.fallguard import (
    FallGuardAssessmentCreate,
    FallGuardAssessmentResponse,
    FallGuardQuestionResponse,
)
from app.services.fallguard import (
    FallGuardService,
    FallGuardSubmissionError,
    assessment_view,
    config_view,
    questions_view,
)

router = APIRouter()

# FallGuard v2.0: the questionnaire is versioned data released with the code (handover PDF SR-14: no hot edits), so the admin
# question editor is retired. The routes stay (same permission) so an old admin client gets an explicit answer, not a 404.
_RETIRED = {"code": "fallguard_questionnaire_is_versioned",
            "message": "The FallGuard questionnaire is versioned and changes only with a release."}


def get_fallguard_service(db: Session = Depends(get_db)) -> FallGuardService:
    return FallGuardService(db)


# Patient: the questionnaire (list form, the older shape plus additive fields)
@router.get("/questions", response_model=List[FallGuardQuestionResponse])
def get_questions(current_user=Depends(get_current_patient)):
    return questions_view()


# Patient: everything the app needs to render the flow (versions, items, red-flag items, fixed messages, emergency number)
@router.get("/config")
def get_config(lang: str = Query("en", description="Only English exists today; every string falls back to English."),
               current_user=Depends(get_current_patient)) -> Dict[str, Any]:
    return config_view()


# Patient: submit assessment
_SUBMIT_ERRORS = {
    422: {"description": "detail = {code, message[, question_id][, fields]}; code is one of fallguard_no_answers, "
                         "fallguard_duplicate_question, fallguard_unknown_question, fallguard_invalid_option, "
                         "fallguard_missing_required, fallguard_date_of_birth_required, fallguard_invalid_date_of_birth, "
                         "fallguard_invalid_assessment_date, fallguard_invalid_payload"},
    429: {"description": "detail.code fallguard_rate_limited (10 submissions per hour per patient)"},
    503: {"description": "detail.code SCORING_UNAVAILABLE: no tier was stored or returned; show the safety screen"},
}


@router.post("/submit", response_model=FallGuardAssessmentResponse, responses=_SUBMIT_ERRORS)
async def submit_assessment(
    data: FallGuardAssessmentCreate,
    current_user=Depends(get_current_patient),
    service: FallGuardService = Depends(get_fallguard_service),
):
    db_patient = service.db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not db_patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    # The server scores; any ``score`` the client sends is ignored, and no tier is stored if scoring fails (SR-3, SR-5).
    try:
        assessment = await service.submit_assessment(db_patient.id, data, db_patient.date_of_birth)
    except FallGuardSubmissionError as exc:
        detail: Dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.question_id is not None:
            detail["question_id"] = str(exc.question_id)
        if exc.fields:
            detail["fields"] = exc.fields
        raise HTTPException(status_code=exc.http_status, detail=detail)
    return assessment_view(assessment)


# Patient: assessment history, newest first
@router.get("/history", response_model=List[FallGuardAssessmentResponse])
def get_history(
    current_user=Depends(get_current_patient),
    service: FallGuardService = Depends(get_fallguard_service),
):
    db_patient = service.db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not db_patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    return [assessment_view(a) for a in service.get_assessment_history(db_patient.id)]


# Admin: the questionnaire is read-only (see _RETIRED)
@router.get("/admin/questions", response_model=List[FallGuardQuestionResponse])
def admin_get_questions(current_user=Depends(require_permission("fallguard.write"))):
    return questions_view()


@router.post("/admin/question", status_code=status.HTTP_410_GONE)
def admin_create_question(current_user=Depends(require_permission("fallguard.write"))):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_RETIRED)


@router.put("/admin/question/{question_id}", status_code=status.HTTP_410_GONE)
def admin_update_question(question_id: str, current_user=Depends(require_permission("fallguard.write"))):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_RETIRED)


@router.delete("/admin/question/{question_id}", status_code=status.HTTP_410_GONE)
def admin_delete_question(question_id: str, current_user=Depends(require_permission("fallguard.write"))):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_RETIRED)
