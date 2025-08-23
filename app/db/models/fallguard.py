import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.db.base import Base
from app.core.security import encrypt, decrypt
import json

class FallGuardQuestion(Base):
    __tablename__ = "fallguard_questions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    question = Column(Text, nullable=False)
    options = Column(Text, nullable=False)  # JSON string: [{"text": ..., "score": ...}, ...]
    order = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class FallGuardAssessment(Base):
    __tablename__ = "fallguard_assessments"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    total_score = Column(Integer, nullable=False)
    ai_summary_encrypted = Column(Text, nullable=True)
    ai_recommendations_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    answers = relationship("FallGuardAnswer", back_populates="assessment")

    @property
    def ai_summary(self):
        return decrypt(self.ai_summary_encrypted) if self.ai_summary_encrypted else None

    @ai_summary.setter
    def ai_summary(self, value):
        self.ai_summary_encrypted = encrypt(value) if value else None

    @property
    def ai_recommendations(self):
        if self.ai_recommendations_encrypted:
            decrypted = decrypt(self.ai_recommendations_encrypted)
            try:
                return json.loads(decrypted)
            except Exception:
                return decrypted
        return None

    @ai_recommendations.setter
    def ai_recommendations(self, value):
        if value is not None:
            self.ai_recommendations_encrypted = encrypt(json.dumps(value))
        else:
            self.ai_recommendations_encrypted = None

class FallGuardAnswer(Base):
    __tablename__ = "fallguard_answers"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("fallguard_assessments.id"), nullable=False)
    question_id = Column(UUID(as_uuid=True), ForeignKey("fallguard_questions.id"), nullable=False)
    answer_encrypted = Column(Text, nullable=False)
    score = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    assessment = relationship("FallGuardAssessment", back_populates="answers")
    question = relationship("FallGuardQuestion")

    @property
    def answer(self):
        return decrypt(self.answer_encrypted)

    @answer.setter
    def answer(self, value):
        self.answer_encrypted = encrypt(value) 