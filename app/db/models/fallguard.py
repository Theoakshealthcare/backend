import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.db.base import Base
from app.core.security import encrypt, decrypt
import json

class FallGuardQuestion(Base):
    """The superseded 24-question screener (v1). FallGuard v2.0 reads its questionnaire from versioned JSON
    (``app/services/fallguard_data``), so nothing reads or writes this table any more. It stays until a later contract
    migration drops it (the handover PDF allows no destructive migration during the pilot)."""
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
    # v2.0: the Part B points total; NULL for an under-60 result (no score, no tier). v1 rows keep their old total.
    total_score = Column(Integer, nullable=True)
    # v2.0 result columns. NULL on v1 rows. Immutable once written (SR-8): trigger ``trg_fallguard_result_immutable``.
    questionnaire_version = Column(String(20), nullable=True)
    algorithm_version = Column(String(20), nullable=True)
    content_version = Column(String(20), nullable=True)
    eligible = Column(Boolean, nullable=True)
    gate_tier = Column(String(16), nullable=True)
    score_tier = Column(String(16), nullable=True)
    final_tier = Column(String(16), nullable=True)
    urgent = Column(Boolean, nullable=True)
    incomplete = Column(Boolean, nullable=True)
    # Sealed JSON (Fernet): the answers as scored, and the full scoring output with its recommendations.
    answers_encrypted = Column(Text, nullable=True)
    result_encrypted = Column(Text, nullable=True)
    ai_summary_encrypted = Column(Text, nullable=True)
    ai_recommendations_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    answers = relationship("FallGuardAnswer", back_populates="assessment")

    @property
    def answers_json(self):
        return json.loads(decrypt(self.answers_encrypted)) if self.answers_encrypted else None

    @answers_json.setter
    def answers_json(self, value):
        self.answers_encrypted = encrypt(json.dumps(value)) if value is not None else None

    @property
    def result_json(self):
        return json.loads(decrypt(self.result_encrypted)) if self.result_encrypted else None

    @result_json.setter
    def result_json(self, value):
        self.result_encrypted = encrypt(json.dumps(value)) if value is not None else None

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