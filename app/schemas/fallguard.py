import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FallGuardOption(BaseModel):
    """One choice. ``text`` is the choice's label (the older field); ``code`` is what v2.0 expects back as the answer.
    ``score`` is the points the choice is worth (null where a code carries no points, e.g. C2)."""
    text: str
    score: Optional[int] = None
    code: Optional[Any] = None


class FallGuardQuestionResponse(BaseModel):
    """One questionnaire item, v2.0. ``id``, ``question``, ``options``, ``order`` and ``is_active`` are the older fields
    (``id`` is now an item id such as ``"A1"``, not a UUID); the rest is additive."""
    id: str
    question: str
    options: List[FallGuardOption]
    order: int
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    part: Optional[str] = None
    type: Optional[str] = None
    required: Optional[bool] = None
    allows_unknown: Optional[bool] = None
    show_if: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    wording_status: Optional[str] = None
    exclusive_codes: Optional[List[str]] = None
    count_bands: Optional[List[Dict[str, Any]]] = None
    age_bands: Optional[List[Dict[str, Any]]] = None
    analytics_only: Optional[bool] = None


class FallGuardAnswerResponse(BaseModel):
    """An answer as stored. ``score`` is the Part B points of that item (null for Part A and C items)."""
    question_id: str
    answer: str
    score: Optional[int] = None
    id: Optional[uuid.UUID] = None  # only on a v1 (24-question) answer

    class Config:
        from_attributes = True


class FallGuardAnswerInput(BaseModel):
    """One answer as the app sends it: ``question_id`` is the item id (``"A1"``, ``"B8"``, ``"R2"``) and ``answer`` is the
    choice's code: an integer for most items, a list of codes for B8, B14 and B17, ``"UNKNOWN"`` for a Part B item the person
    cannot or will not answer, ``true`` or ``false`` for a red-flag item R1 to R7, a list of text for C7.

    ``score`` is accepted so the clients already shipped keep working, but it is IGNORED: the server scores the answers itself.
    """
    question_id: str
    answer: Any
    score: Optional[int] = Field(
        default=None, description="Ignored. Accepted for older clients; the server scores from the stored questionnaire.")


class FallGuardAssessmentCreate(BaseModel):
    date_of_birth: Optional[date] = Field(
        default=None, description="Used for the age. Left out: the date of birth on the patient's profile is used.")
    answers: List[FallGuardAnswerInput]


class FallGuardAssessmentResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    total_score: Optional[int] = None
    ai_summary: Optional[str] = None
    ai_recommendations: Optional[List[str]] = None
    created_at: datetime
    answers: List[FallGuardAnswerResponse]
    # v2.0, additive. Null on a v1 (24-question) assessment.
    questionnaire_version: Optional[str] = None
    algorithm_version: Optional[str] = None
    content_version: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    recommendations: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ai_review_status: Optional[str] = None

    class Config:
        from_attributes = True
