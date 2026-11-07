import json
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from app.db.models.fallguard import FallGuardQuestion, FallGuardAssessment, FallGuardAnswer
from app.db.models.patient import Patient
from app.services.gemini import gemini_service
from app.schemas.fallguard import FallGuardAssessmentCreate

class FallGuardSubmissionError(Exception):
    """A submission the server refuses (HTTP 422). ``code`` and ``message`` are stable: the app may switch on them."""

    def __init__(self, code: str, message: str, question_id=None):
        super().__init__(message)
        self.code, self.message, self.question_id = code, message, question_id


class FallGuardService:
    def __init__(self, db: Session):
        self.db = db

    # Admin: CRUD for questions
    def create_question(self, data: Dict[str, Any]) -> FallGuardQuestion:
        question = FallGuardQuestion(
            question=data['question'],
            options=json.dumps(data['options']),
            order=data['order'],
            is_active=data.get('is_active', True)
        )
        self.db.add(question)
        self.db.commit()
        self.db.refresh(question)
        return question

    def update_question(self, question_id, data: Dict[str, Any]) -> FallGuardQuestion:
        question = self.db.query(FallGuardQuestion).filter(FallGuardQuestion.id == question_id).first()
        if not question:
            raise Exception('Question not found')
        question.question = data['question']
        question.options = json.dumps([opt.dict() for opt in data['options']])
        question.order = data['order']
        question.is_active = data.get('is_active', True)
        self.db.commit()
        self.db.refresh(question)
        return question

    def delete_question(self, question_id):
        question = self.db.query(FallGuardQuestion).filter(FallGuardQuestion.id == question_id).first()
        if question:
            self.db.delete(question)
            self.db.commit()

    def get_questions(self, active_only=True) -> List[FallGuardQuestion]:
        q = self.db.query(FallGuardQuestion)
        if active_only:
            q = q.filter(FallGuardQuestion.is_active == True)
        return q.order_by(FallGuardQuestion.order).all()

    def score_answers(self, data: FallGuardAssessmentCreate) -> List[Dict[str, Any]]:
        """Resolve each answer against the stored question: returns [{question_id, answer, score}] with the server's
        score and the stored option text. Raises ``FallGuardSubmissionError`` (never a 500) for an empty list, a
        repeated question, an unknown or inactive question, or an answer that is not one of the question's options.
        The client's own ``score`` is never read."""
        if not data.answers:
            raise FallGuardSubmissionError("fallguard_no_answers", "Answer at least one question.")
        seen = set()
        for ans in data.answers:
            if ans.question_id in seen:
                raise FallGuardSubmissionError(
                    "fallguard_duplicate_question", "Each question can only be answered once.", ans.question_id)
            seen.add(ans.question_id)
        questions = {
            q.id: q for q in self.db.query(FallGuardQuestion).filter(
                FallGuardQuestion.id.in_(seen), FallGuardQuestion.is_active == True)  # noqa: E712
        }
        scored = []
        for ans in data.answers:
            question = questions.get(ans.question_id)
            if question is None:
                raise FallGuardSubmissionError(
                    "fallguard_unknown_question", "One of the questions is not part of the assessment.", ans.question_id)
            option = next((o for o in json.loads(question.options) if o.get("text") == ans.answer.strip()), None)
            if option is None:
                raise FallGuardSubmissionError(
                    "fallguard_invalid_option", "That answer is not one of the choices for the question.", ans.question_id)
            scored.append({"question_id": question.id, "answer": option["text"], "score": int(option["score"])})
        return scored

    # Patient: submit assessment
    async def submit_assessment(self, patient_id, data: FallGuardAssessmentCreate) -> FallGuardAssessment:
        scored = self.score_answers(data)
        total_score = sum(a["score"] for a in scored)
        # One transaction for the assessment and its answers: a failure cannot leave a score with no answers.
        assessment = FallGuardAssessment(patient_id=patient_id, total_score=total_score)
        self.db.add(assessment)
        self.db.flush()
        for a in scored:
            answer = FallGuardAnswer(assessment_id=assessment.id, question_id=a["question_id"], score=a["score"])
            answer.answer = a["answer"]  # uses encryption property
            self.db.add(answer)
        self.db.commit()
        # AI analysis (optional: the Gemini service falls back to "Unable to analyze" without a key)
        answers_for_ai = [{**a, "question_id": str(a["question_id"])} for a in scored]
        ai_result = await gemini_service.analyze_fallguard_assessment(answers_for_ai, total_score)
        assessment.ai_summary = ai_result.get('summary')
        assessment.ai_recommendations = ai_result.get('recommendations')
        self.db.commit()
        self.db.refresh(assessment)
        return assessment

    def get_assessment_history(self, patient_id) -> List[FallGuardAssessment]:
        return self.db.query(FallGuardAssessment).filter(FallGuardAssessment.patient_id == patient_id).order_by(FallGuardAssessment.created_at.desc()).all()

    def get_assessment(self, assessment_id) -> FallGuardAssessment:
        return self.db.query(FallGuardAssessment).filter(FallGuardAssessment.id == assessment_id).first() 