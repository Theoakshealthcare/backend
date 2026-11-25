"""FallGuard v2.0 for the patient app: serve the questionnaire, score a submission on the server, store the immutable result.

The questionnaire is versioned data (``fallguard_data/questionnaire_v2_0.json``), not database rows: it changes only through a
code release (SR-14), so there is no seed script and no admin editing. The scoring itself is the pure module
``fallguard_scoring``; this file is the adapter: it turns the app's ``answers`` list into the library's input, maps the library's
refusals to a stable 422, stores the result in one transaction, and never stores or returns a tier when scoring fails (SR-5).
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.fallguard import FallGuardAssessment
from app.schemas.fallguard import FallGuardAssessmentCreate
from app.services import fallguard_questionnaire as questionnaire
from app.services import fallguard_recommendations as recommendations
from app.services import fallguard_scoring as scoring
from app.services.fallguard_questionnaire import (  # noqa: F401  (used by the endpoints)
    config_view,
    questions_view,
)
from app.services.gemini import gemini_service

log = logging.getLogger(__name__)

SUBMIT_RATE_LIMIT_PER_HOUR = 10  # handover PDF section 17: "Submit: 10 per hour per subject"
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
        """The scoring input from the app's answers list. Everything the reference library does not check (it assumes a
        well-formed request) is checked here, so a bad body is a 422 and never a silent mis-score or a 503."""
        if not data.answers:
            raise FallGuardSubmissionError("fallguard_no_answers", "Answer at least one question.")
        parts: Dict[str, Dict[str, Any]] = {"part_a": {}, "part_b": {}, "part_c": {}, "red_flags": {}}
        seen = set()
        for ans in data.answers:
            qid = ans.question_id
            if qid in seen:
                raise FallGuardSubmissionError(
                    "fallguard_duplicate_question", "Each question can only be answered once.", qid)
            seen.add(qid)
            item = questionnaire.ITEMS.get(qid)
            if qid in questionnaire.RED_FLAG_IDS:
                if not isinstance(ans.answer, bool):
                    raise _invalid_answer(qid, "A red-flag answer is true or false.")
                parts["red_flags"][qid] = ans.answer
            elif item is None or item["type"] == "derived":  # B1 is worked out from the date of birth, never asked
                raise FallGuardSubmissionError(
                    "fallguard_unknown_question", "One of the questions is not part of the assessment.", qid)
            else:
                parts[f"part_{item['part'].lower()}"][qid] = _check_answer_shape(item, ans.answer)
        dob = data.date_of_birth or date_of_birth
        if dob is None:
            raise FallGuardSubmissionError(
                "fallguard_date_of_birth_required", "Date of birth is needed to work out the age.",
                fields={"date_of_birth": "required"})
        today = assessment_date()
        if dob > today:  # the reference library would call a negative age "under 60", so refuse it here
            raise FallGuardSubmissionError(
                "fallguard_invalid_date_of_birth", "Date of birth is in the future.", fields={"date_of_birth": "future"})
        return {"date_of_birth": dob.isoformat(), "assessment_date": today.isoformat(), **parts}

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
        except scoring.ValidationError as exc:  # the request is wrong: 422 with the library's code (PDF section 8)
            raise _library_error(exc) from exc
        except Exception as exc:  # SR-5: scoring failed, so no tier is stored or returned
            log.error("FallGuard scoring failed: %s", type(exc).__name__)
            raise _scoring_unavailable() from exc
        try:
            recs = recommendations.split_top(recommendations.applicable(payload, result))
            assessment = FallGuardAssessment(
                patient_id=patient_id, total_score=result["score"],
                questionnaire_version=result["questionnaire_version"], algorithm_version=result["algorithm_version"],
                content_version=questionnaire.CONTENT_VERSION, eligible=result["eligible"],
                gate_tier=result["gate_tier"], score_tier=result["score_tier"], final_tier=result["final_tier"],
                urgent=result["urgent"], incomplete=result.get("incomplete", False))
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
                 for i, a in payload[part].items() if i not in ("C7", "C8")]
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


_LIBRARY_MESSAGES = {
    "INVALID_ANSWER": "That answer is not one of the choices for the question.",
    "EXCLUSIVE_ANSWER": "\"None of these\" and \"I am not sure\" cannot be combined with another choice.",
    "MISSING_REQUIRED": "Answer every required question.",
    "INVALID_DATE": "That is not a calendar date (YYYY-MM-DD).",
    "INVALID_RED_FLAG": "That is not a red-flag item.",
}


def _invalid_answer(item_id: str, message: str = _LIBRARY_MESSAGES["INVALID_ANSWER"]) -> FallGuardSubmissionError:
    return FallGuardSubmissionError("INVALID_ANSWER", message, item_id, {item_id: "INVALID_ANSWER"})


def _library_error(exc: "scoring.ValidationError") -> FallGuardSubmissionError:
    """The reference library's refusal as a 422 with its own code. It names the item first in its detail text."""
    detail = str(exc).split(": ", 1)[-1]
    found = re.match(r"[A-Z]\d+", detail)
    item_id = found.group(0) if found else ("date_of_birth" if exc.code == "INVALID_DATE" else None)
    return FallGuardSubmissionError(
        exc.code, _LIBRARY_MESSAGES.get(exc.code, "The assessment could not be scored."),
        item_id if found else None, {item_id: exc.code} if item_id else None)


def _check_answer_shape(item: Dict[str, Any], value: Any) -> Any:
    """What the reference library does not check. Part A and the Part B choices are range-checked by the library; here the
    types it would trip over (a list inside a list) and the whole of Part C, which it never reads for validity."""
    kind, qid = item["type"], item["id"]
    if value is None:
        return None
    if kind == "multi":
        if value != "UNKNOWN" and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise _invalid_answer(qid)
    elif kind == "single" and item["part"] == "C":
        codes = [o["code"] for o in item["options"]]
        if not any(type(value) is type(c) and value == c for c in codes):
            raise _invalid_answer(qid)
    elif kind == "text_list":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _invalid_answer(qid)
    elif kind == "demographics":
        fields = item["fields"]
        if not isinstance(value, dict) or any(k not in fields or v not in fields[k] for k, v in value.items()):
            raise _invalid_answer(qid)
    return value


def _scoring_unavailable() -> FallGuardSubmissionError:
    # error.safety is the PDF's fixed Appendix C text (SR-5); the app shows its own copy of it with the emergency details.
    return FallGuardSubmissionError(
        "SCORING_UNAVAILABLE", questionnaire.CONTENT["messages"]["error.safety"], http_status=503)
