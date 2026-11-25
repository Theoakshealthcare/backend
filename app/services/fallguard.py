"""FallGuard v2.0 for the patient app: serve the questionnaire, score a submission on the server, store the immutable result.

The questionnaire is versioned data (``fallguard_data/questionnaire_v2_0.json``), not database rows: it changes only through a
code release (SR-14), so there is no seed script and no admin editing. The scoring itself is the pure module
``fallguard_scoring``; this file is the adapter: it turns the app's ``answers`` list into the library's input, maps the library's
refusals to a stable 422, stores the result in one transaction, and never stores or returns a tier when scoring fails (SR-5).
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.fallguard import FallGuardAssessment
from app.schemas.fallguard import FallGuardAssessmentCreate
from app.services import fallguard_recommendations as recommendations
from app.services import fallguard_scoring as scoring
from app.services.gemini import gemini_service

log = logging.getLogger(__name__)

SUBMIT_RATE_LIMIT_PER_HOUR = 10  # handover PDF section 17: "Submit: 10 per hour per subject"
WORDING_PENDING = "PENDING_CLINICAL_WORDING"
AI_PENDING_REVIEW = "PENDING_CLINICIAN_REVIEW"  # the PDF and the owner: AI-generated FallGuard text is reviewed before launch


class FallGuardSubmissionError(Exception):
    """A submission the server refuses. ``code`` and ``message`` are stable: the app may switch on them. HTTP 422 unless
    ``http_status`` says otherwise (429 rate limit, 503 scoring unavailable)."""

    def __init__(self, code: str, message: str, question_id=None, fields: Optional[Dict[str, str]] = None,
                 http_status: int = 422):
        super().__init__(message)
        self.code, self.message, self.question_id = code, message, question_id
        self.fields, self.http_status = dict(fields or {}), http_status


def assessment_date() -> date:
    """The assessment date is today in Africa/Lagos (PDF section 17), decided by the server."""
    return datetime.now(ZoneInfo(settings.HOME_TIMEZONE)).date()


# --- the questionnaire as the app receives it --------------------------------------------------------------------------------

def question_view(item: Dict[str, Any], order: int) -> Dict[str, Any]:
    wording_ready = item.get("text") is not None
    return {
        "id": item["id"],
        "question": item.get("text") or item.get("summary") or f"Item {item['id']}",
        "options": [{"text": o.get("label") or str(o["code"]), "score": o.get("points"), "code": o["code"]}
                    for o in item["options"]],
        "order": order,
        "is_active": True,
        "part": item["part"],
        "type": item["type"],
        "required": item["required"],
        "allows_unknown": item["allows_unknown"],
        "show_if": item["show_if"],
        "summary": item.get("summary"),
        "wording_status": "APPROVED" if wording_ready else WORDING_PENDING,
        "exclusive_codes": item.get("exclusive_codes"),
        "count_bands": item.get("count_bands"),
        "age_bands": item.get("age_bands"),
        "analytics_only": item.get("analytics_only"),
    }


def questions_view() -> List[Dict[str, Any]]:
    return [question_view(item, n) for n, item in enumerate(scoring.QUESTIONNAIRE["items"], start=1)]


def config_view() -> Dict[str, Any]:
    """Everything the app needs to render the questionnaire, the red-flag items and the fixed messages."""
    content = recommendations.CONTENT
    return {
        "questionnaire_version": scoring.QUESTIONNAIRE_VERSION,
        "algorithm_version": scoring.ALGORITHM_VERSION,
        "content_version": recommendations.CONTENT_VERSION,
        "language": "en",
        "wording_status": scoring.QUESTIONNAIRE["wording_status"],
        "content_sign_off_status": content["sign_off_status"],
        "emergency_number": content["emergency_number"]["default"],
        "assessment_minutes": content["assessment_minutes"],
        "min_age": scoring.QUESTIONNAIRE["scoring"]["min_age"],
        "items": questions_view(),
        "red_flags": scoring.QUESTIONNAIRE["red_flags"],
        "messages": content["messages"],
        "welcome": content["welcome"],
    }


# --- stored answers as the app reads them back --------------------------------------------------------------------------------

def _answer_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return ",".join(f"{k}={v}" for k, v in sorted(value.items()))
    return str(value)


def answers_view(assessment: FallGuardAssessment) -> List[Dict[str, Any]]:
    stored = assessment.answers_json
    if stored is None:  # a v1 (24-question) assessment: its answers are in the old table
        return [{"id": a.id, "question_id": str(a.question_id), "answer": a.answer, "score": a.score}
                for a in assessment.answers]
    points = (assessment.result_json or {}).get("scoring", {}).get("part_b_points", {})
    out = []
    for part in ("part_a", "part_b", "part_c", "red_flags"):
        for item_id, value in (stored.get(part) or {}).items():
            out.append({"question_id": item_id, "answer": _answer_text(value),
                        "score": points.get(item_id) if part == "part_b" else None})
    return out


def assessment_view(assessment: FallGuardAssessment) -> Dict[str, Any]:
    stored = assessment.result_json
    return {
        "id": assessment.id,
        "patient_id": assessment.patient_id,
        "total_score": assessment.total_score,
        "ai_summary": assessment.ai_summary,
        "ai_recommendations": assessment.ai_recommendations,
        "created_at": assessment.created_at,
        "answers": answers_view(assessment),
        "questionnaire_version": assessment.questionnaire_version,
        "algorithm_version": assessment.algorithm_version,
        "content_version": assessment.content_version,
        "result": stored["scoring"] if stored else None,
        "recommendations": stored["recommendations"] if stored else None,
        "ai_review_status": AI_PENDING_REVIEW if assessment.ai_summary and stored else None,
    }


class FallGuardService:
    def __init__(self, db: Session):
        self.db = db

    # --- building the scoring input from the app's answers list -----------------------------------------------------------

    @staticmethod
    def build_payload(data: FallGuardAssessmentCreate, date_of_birth: Optional[date]) -> Dict[str, Any]:
        if not data.answers:
            raise FallGuardSubmissionError("fallguard_no_answers", "Answer at least one question.")
        parts: Dict[str, Dict[str, Any]] = {"part_a": {}, "part_b": {}, "part_c": {}, "red_flags": {}}
        seen = set()
        for ans in data.answers:
            if ans.question_id in seen:
                raise FallGuardSubmissionError(
                    "fallguard_duplicate_question", "Each question can only be answered once.", ans.question_id)
            seen.add(ans.question_id)
            if ans.question_id in scoring.RED_FLAG_IDS:
                parts["red_flags"][ans.question_id] = ans.answer
            elif ans.question_id in scoring.ITEMS:
                parts[f"part_{scoring.ITEMS[ans.question_id]['part'].lower()}"][ans.question_id] = ans.answer
            else:
                raise FallGuardSubmissionError(
                    "fallguard_unknown_question", "One of the questions is not part of the assessment.", ans.question_id)
        dob = data.date_of_birth or date_of_birth
        if dob is None:
            raise FallGuardSubmissionError(
                "fallguard_date_of_birth_required", "Date of birth is needed to work out the age.",
                fields={"date_of_birth": "required"})
        return {"date_of_birth": dob.isoformat(), "assessment_date": assessment_date().isoformat(), **parts}

    def _check_rate_limit(self, patient_id) -> None:
        since = datetime.utcnow() - timedelta(hours=1)
        recent = self.db.query(FallGuardAssessment).filter(
            FallGuardAssessment.patient_id == patient_id, FallGuardAssessment.created_at >= since).count()
        if recent >= SUBMIT_RATE_LIMIT_PER_HOUR:
            raise FallGuardSubmissionError(
                "fallguard_rate_limited", "Too many assessments in the last hour. Please try again later.",
                http_status=429)

    # --- patient: submit ---------------------------------------------------------------------------------------------------

    async def submit_assessment(self, patient_id, data: FallGuardAssessmentCreate,
                                profile_date_of_birth: Optional[date] = None) -> FallGuardAssessment:
        payload = self.build_payload(data, profile_date_of_birth)
        self._check_rate_limit(patient_id)
        try:
            result = scoring.score_assessment(payload)
        except scoring.ScoringError as exc:  # the request is wrong: 422 with the library's code (PDF section 8)
            raise FallGuardSubmissionError(exc.code, exc.message, exc.question_id, exc.fields) from exc
        except Exception as exc:  # SR-5: scoring failed, so no tier is stored or returned
            log.error("FallGuard scoring failed: %s", type(exc).__name__)
            raise _scoring_unavailable() from exc
        try:
            recs = recommendations.split_top(recommendations.applicable(payload, result))
            assessment = FallGuardAssessment(
                patient_id=patient_id, total_score=result["score"],
                questionnaire_version=result["questionnaire_version"], algorithm_version=result["algorithm_version"],
                content_version=recommendations.CONTENT_VERSION, eligible=result["eligible"],
                gate_tier=result["gate_tier"], score_tier=result["score_tier"], final_tier=result["final_tier"],
                urgent=result["urgent"], incomplete=result["incomplete"])
            assessment.answers_json = payload
            assessment.result_json = {"scoring": result, "recommendations": recs}
            self.db.add(assessment)
            self.db.commit()  # the result is stored whole or not at all
        except Exception as exc:
            self.db.rollback()
            log.error("FallGuard result could not be stored: %s", type(exc).__name__)
            raise _scoring_unavailable() from exc
        if settings.FALLGUARD_AI_ADVICE_ENABLED:
            await self._add_ai_advice(assessment, payload, result)
        self.db.refresh(assessment)
        return assessment

    async def _add_ai_advice(self, assessment: FallGuardAssessment, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Optional and off by default. Never fails the submission; never sends the date of birth or free text."""
        coded = [{"question_id": i, "answer": a} for part in ("part_a", "part_b", "part_c")
                 for i, a in payload[part].items() if i not in ("C7", "C8", "C9")]
        try:
            ai = await gemini_service.analyze_fallguard_assessment(coded, result["score"] or 0)
            assessment.ai_summary, assessment.ai_recommendations = ai.get("summary"), ai.get("recommendations")
        except Exception as exc:
            log.warning("FallGuard AI advice unavailable: %s", type(exc).__name__)
            return
        self.db.commit()

    def get_assessment_history(self, patient_id) -> List[FallGuardAssessment]:
        return (self.db.query(FallGuardAssessment).filter(FallGuardAssessment.patient_id == patient_id)
                .order_by(FallGuardAssessment.created_at.desc()).all())

    def get_assessment(self, assessment_id) -> Optional[FallGuardAssessment]:
        return self.db.query(FallGuardAssessment).filter(FallGuardAssessment.id == assessment_id).first()


def _scoring_unavailable() -> FallGuardSubmissionError:
    # error.safety is the PDF's fixed Appendix C text (SR-5); the app shows its own copy of it with the emergency details.
    return FallGuardSubmissionError(
        "SCORING_UNAVAILABLE", recommendations.CONTENT["messages"]["error.safety"], http_status=503)
