import json
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from app.db.models.fallguard import FallGuardQuestion, FallGuardAssessment, FallGuardAnswer
from app.db.models.patient import Patient
from app.services.gemini import gemini_service
from app.schemas.fallguard import FallGuardAssessmentCreate

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

    # Patient: submit assessment
    async def submit_assessment(self, patient_id, data: FallGuardAssessmentCreate) -> FallGuardAssessment:
        total_score = sum(ans.score for ans in data.answers)
        assessment = FallGuardAssessment(
            patient_id=patient_id,
            total_score=total_score
        )
        self.db.add(assessment)
        self.db.commit()
        self.db.refresh(assessment)
        # Store answers
        for ans in data.answers:
            answer = FallGuardAnswer(
                assessment_id=assessment.id,
                question_id=ans.question_id,
                score=ans.score
            )
            answer.answer = ans.answer  # uses encryption property
            self.db.add(answer)
        self.db.commit()
        # AI analysis
        answers_for_ai = [
            {
                "question_id": str(ans.question_id),
                "answer": ans.answer,
                "score": ans.score
            } for ans in data.answers
        ]
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