import uuid
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

class FallGuardOption(BaseModel):
    text: str
    score: int

class FallGuardQuestionBase(BaseModel):
    question: str
    options: List[FallGuardOption]
    order: int
    is_active: Optional[bool] = True

class FallGuardQuestionCreate(FallGuardQuestionBase):
    pass

class FallGuardQuestionUpdate(FallGuardQuestionBase):
    pass

class FallGuardQuestionResponse(FallGuardQuestionBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class FallGuardAnswerBase(BaseModel):
    question_id: uuid.UUID
    answer: str
    score: int

class FallGuardAnswerResponse(FallGuardAnswerBase):
    id: uuid.UUID
    class Config:
        from_attributes = True

class FallGuardAnswerInput(BaseModel):
    """One answer as the patient app sends it.

    ``score`` is accepted so the clients already shipped keep working, but it is IGNORED: the server looks the score
    up from the stored option whose ``text`` equals ``answer`` and computes the total itself.
    """
    question_id: uuid.UUID
    answer: str
    score: Optional[int] = Field(
        default=None, description="Ignored. Accepted for older clients; the server scores from the stored options.")

class FallGuardAssessmentCreate(BaseModel):
    answers: List[FallGuardAnswerInput]

class FallGuardAssessmentResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    total_score: int
    ai_summary: Optional[str]
    ai_recommendations: Optional[List[str]]
    created_at: datetime
    answers: List[FallGuardAnswerResponse]
    class Config:
        from_attributes = True 