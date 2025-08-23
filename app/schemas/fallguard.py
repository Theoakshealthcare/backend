import uuid
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel

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

class FallGuardAssessmentCreate(BaseModel):
    answers: List[FallGuardAnswerBase]

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