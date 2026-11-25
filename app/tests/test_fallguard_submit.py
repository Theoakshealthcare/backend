"""POST /fallguard/submit, GET /questions, /config and /history for FallGuard v2.0.

The pure scoring rules are tested in test_fallguard_scoring.py (its expected values come from the handover PDF). These tests
cover the adapter: the answers list becomes the scoring input, a bad request is a 422 with a stable code, a result is stored
whole and sealed and cannot be changed (SR-8), a scoring failure stores and returns no tier (SR-5), the client's own score
is never read, and the optional AI summary is off by default and flagged for review when on.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.security import decrypt
from app.db.models.fallguard import FallGuardAssessment
from app.services import fallguard as fallguard_service
from app.services import fallguard_scoring as scoring
from app.tests.factories import auth_headers, make_admin, make_caregiver, make_patient

API = "/api/v1/fallguard"
TODAY = date(2026, 11, 10)

# Appendix A's answers (the PDF's one worked example) as the app sends them.
APPENDIX_A_ANSWERS = [
    {"question_id": "A1", "answer": 1}, {"question_id": "A2", "answer": 0}, {"question_id": "A3", "answer": 0},
    {"question_id": "A4", "answer": 0}, {"question_id": "A5", "answer": 1}, {"question_id": "A6", "answer": 2},
    {"question_id": "A7", "answer": 0},
    {"question_id": "B2", "answer": 1}, {"question_id": "B3", "answer": 1}, {"question_id": "B4", "answer": 1},
    {"question_id": "B5", "answer": 2}, {"question_id": "B6", "answer": 1}, {"question_id": "B7", "answer": 2},
    {"question_id": "B8", "answer": ["DIABETES", "ARTHRITIS"]}, {"question_id": "B9", "answer": 1},
    {"question_id": "B10", "answer": 0}, {"question_id": "B11", "answer": 1}, {"question_id": "B12", "answer": 0},
    {"question_id": "B13", "answer": 1}, {"question_id": "B14", "answer": ["CARDIO_BP_DIURETIC"]},
    {"question_id": "B15", "answer": 1}, {"question_id": "B16", "answer": 0},
    {"question_id": "B17", "answer": ["LOOSE_RUGS", "NO_BACKUP_LIGHT"]}, {"question_id": "B18", "answer": 1},
    {"question_id": "B19", "answer": 0},
    {"question_id": "C1", "answer": 1}, {"question_id": "C2", "answer": "SOMETIMES"},
    {"question_id": "C5", "answer": 0}, {"question_id": "C6", "answer": 0},
]
LOW_ANSWERS = [{"question_id": q, "answer": 0} for q in ("A1", "A4", "A5", "A6")]  # Part A only: Part B is all unknown
ZERO_B_ANSWERS = [{"question_id": f"B{n}", "answer": 0} for n in (2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 15, 16, 18, 19)] + [
    {"question_id": q, "answer": ["NONE"]} for q in ("B8", "B14", "B17")]
LOW_FULL = LOW_ANSWERS + ZERO_B_ANSWERS  # every best answer: with a 66-year-old this scores 1 and the final tier is LOW


class FakeGemini:
    def __init__(self):
        self.calls = []

    async def analyze_fallguard_assessment(self, answers, total_score):
        self.calls.append((answers, total_score))
        return {"summary": "fake summary", "recommendations": ["fake advice"]}


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(fallguard_service, "assessment_date", lambda: TODAY)


@pytest.fixture
def gemini(monkeypatch):
    fake = FakeGemini()
    monkeypatch.setattr(fallguard_service, "gemini_service", fake)
    return fake


@pytest.fixture
def patient(db):
    return make_patient(db, date_of_birth=date(1946, 5, 20))


def submit(client, user, answers, **extra):
    return client.post(f"{API}/submit", json={"answers": answers, **extra}, headers=auth_headers(user))


def code(response):
    return response.json()["detail"]["code"]


# --- the worked example end to end --------------------------------------------------------------------------------------

def test_appendix_a_answers_submitted_through_the_api_give_the_pdfs_result(client, patient):
    r = submit(client, patient.user, APPENDIX_A_ANSWERS)
    assert r.status_code == 200, r.text
    body = r.json()
    result = body["result"]
    assert (result["age"], result["score"], result["gate_tier"], result["score_tier"], result["final_tier"]) == (
        80, 18, "INTERMEDIATE", "HIGH", "HIGH")
    assert result["gate_reasons"] == ["GATE_I_ONE_FALL", "GATE_I_UNSTEADY", "GATE_I_WORRY"]
    assert result["top_factors"] == ["B5", "B7", "B2"] and result["consequence_flag"] is True
    assert body["total_score"] == 18 and body["questionnaire_version"] == "2.0.0" and body["algorithm_version"] == "2.0.0"
    assert body["content_version"] == "2.0.0"
    assert [r["id"] for r in body["recommendations"]["top"]] == ["REC-CLINICAL", "REC-EXERCISE", "REC-BOOK-NURSE"]
    assert len(body["recommendations"]["all"]) == 15
    assert body["ai_summary"] is None and body["ai_review_status"] is None
    by_id = {a["question_id"]: a for a in body["answers"]}
    assert by_id["B8"] == {"question_id": "B8", "answer": "DIABETES,ARTHRITIS", "score": 1, "id": None}
    assert by_id["A1"]["score"] is None and by_id["C2"]["answer"] == "SOMETIMES"


def test_the_date_of_birth_in_the_body_beats_the_profile_and_is_used_when_the_profile_has_none(db, client):
    bare = make_patient(db)  # no date of birth on the profile
    r = submit(client, bare.user, LOW_ANSWERS)
    assert r.status_code == 422 and code(r) == "fallguard_date_of_birth_required"
    r = submit(client, bare.user, LOW_ANSWERS, date_of_birth="1960-06-15")
    assert r.status_code == 200 and r.json()["result"]["age"] == 66
    r = submit(client, bare.user, LOW_ANSWERS, date_of_birth="1930-01-01")
    assert r.json()["result"]["age"] == 96 and r.json()["result"]["part_b_points"]["B1"] == 3


def test_an_under_60_submission_has_no_score_no_tier_and_no_recommendations(db, client, patient):
    r = submit(client, patient.user, LOW_ANSWERS, date_of_birth="1990-01-01")
    assert r.status_code == 200
    body = r.json()
    assert body["total_score"] is None and body["result"]["eligible"] is False and body["result"]["final_tier"] is None
    assert body["recommendations"] == {"top": [], "all": []}
    row = db.query(FallGuardAssessment).one()
    assert (row.eligible, row.final_tier, row.gate_tier, row.score_tier, row.total_score) == (False, None, None, None, None)


def test_a_red_flag_answer_makes_the_result_urgent_without_changing_the_tier(client, patient):
    r = submit(client, patient.user, [*LOW_FULL, {"question_id": "R3", "answer": True}, {"question_id": "R2", "answer": "no"}],
               date_of_birth="1960-06-15")
    result = r.json()["result"]
    assert result["urgent"] is True and result["red_flags_triggered"] == ["R3"]
    assert (result["score"], result["final_tier"]) == (1, "LOW")  # Urgent never moves the tier


# --- storage ------------------------------------------------------------------------------------------------------------------

def test_the_result_is_stored_sealed_with_its_versions_and_tiers(db, client, patient):
    submit(client, patient.user, APPENDIX_A_ANSWERS)
    row = db.query(FallGuardAssessment).one()
    assert (row.questionnaire_version, row.algorithm_version, row.content_version) == ("2.0.0", "2.0.0", "2.0.0")
    assert (row.gate_tier, row.score_tier, row.final_tier, row.total_score) == ("INTERMEDIATE", "HIGH", "HIGH", 18)
    assert (row.urgent, row.incomplete, row.eligible) == (False, False, True)
    assert "DIABETES" not in row.answers_encrypted and "HIGH" not in row.result_encrypted  # sealed, not plain text
    assert decrypt(row.answers_encrypted).count("DIABETES") == 1
    assert row.answers_json["part_a"]["A1"] == 1 and row.answers_json["assessment_date"] == "2026-11-10"
    assert row.answers_json["date_of_birth"] == "1946-05-20"  # a later profile change cannot alter a past result


def test_a_stored_result_cannot_be_changed_but_the_ai_columns_can(db, client, patient):
    submit(client, patient.user, APPENDIX_A_ANSWERS)
    row = db.query(FallGuardAssessment).one()
    for column, value in (("final_tier", "LOW"), ("total_score", 0), ("urgent", True), ("algorithm_version", "9.9.9"),
                          ("questionnaire_version", "9.9.9"), ("content_version", "9.9.9"), ("incomplete", True),
                          ("eligible", False), ("gate_tier", "LOW"), ("score_tier", "LOW")):
        with pytest.raises(DBAPIError, match="immutable"):
            db.execute(text(f"UPDATE fallguard_assessments SET {column} = :v WHERE id = :id"), {"v": value, "id": row.id})
        db.rollback()
    row = db.get(FallGuardAssessment, row.id)
    row.ai_summary = "added later"  # the optional AI text is written after the score, so it stays writable
    db.commit()
    assert db.get(FallGuardAssessment, row.id).ai_summary == "added later"


def test_history_is_newest_first_and_carries_the_v2_result(client, patient):
    first = submit(client, patient.user, LOW_FULL, date_of_birth="1960-06-15").json()
    second = submit(client, patient.user, APPENDIX_A_ANSWERS).json()
    history = client.get(f"{API}/history", headers=auth_headers(patient.user)).json()
    assert [h["id"] for h in history] == [second["id"], first["id"]]
    assert [h["result"]["final_tier"] for h in history] == ["HIGH", "LOW"]
    assert history[0]["recommendations"]["all"] == second["recommendations"]["all"]


def test_history_is_only_the_patients_own(db, client, patient):
    other = make_patient(db, date_of_birth=date(1950, 1, 1))
    submit(client, other.user, LOW_ANSWERS)
    assert client.get(f"{API}/history", headers=auth_headers(patient.user)).json() == []


# --- bad requests are a 422 with a stable code and store nothing -----------------------------------------------------------------

def refused(db, client, patient, answers, **extra):
    r = submit(client, patient.user, answers, **extra)
    assert db.query(FallGuardAssessment).count() == 0
    return r


def test_an_empty_answer_list_is_a_422(db, client, patient):
    r = refused(db, client, patient, [])
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "fallguard_no_answers", "message": "Answer at least one question."}


def test_the_same_question_twice_is_a_422(db, client, patient):
    r = refused(db, client, patient, [*LOW_ANSWERS, {"question_id": "A1", "answer": 2}])
    assert r.status_code == 422 and code(r) == "fallguard_duplicate_question" and r.json()["detail"]["question_id"] == "A1"


@pytest.mark.parametrize("question_id", ["A99", "B1", str(uuid.uuid4()), "R8", ""])
def test_an_unknown_question_is_a_422_not_a_500(db, client, patient, question_id):
    r = refused(db, client, patient, [*LOW_ANSWERS, {"question_id": question_id, "answer": 0}])
    assert r.status_code == 422 and code(r) == "fallguard_unknown_question"


def test_an_answer_that_is_not_a_choice_is_a_422_and_names_the_field(db, client, patient):
    r = refused(db, client, patient, [{"question_id": "A1", "answer": 7}, *LOW_ANSWERS[1:]])
    assert r.status_code == 422 and code(r) == "fallguard_invalid_option"
    assert r.json()["detail"]["question_id"] == "A1" and r.json()["detail"]["fields"] == {"A1": "invalid_option"}


def test_a_missing_required_answer_is_a_422_listing_every_missing_item(db, client, patient):
    r = refused(db, client, patient, [{"question_id": "A1", "answer": 1}, {"question_id": "A4", "answer": 0}])
    assert r.status_code == 422 and code(r) == "fallguard_missing_required"
    assert r.json()["detail"]["fields"] == {k: "required" for k in ("A5", "A6", "A2", "A3", "A7")}


def test_a_future_date_of_birth_is_a_422(db, client, patient):
    r = refused(db, client, patient, LOW_ANSWERS, date_of_birth="2030-01-01")
    assert r.status_code == 422 and code(r) == "fallguard_invalid_date_of_birth"


def test_malformed_bodies_stay_a_pydantic_422(client, patient):
    r = client.post(f"{API}/submit", json={"answers": [{"answer": 0}]}, headers=auth_headers(patient.user))
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_a_score_the_client_makes_up_is_ignored(client, patient):
    honest = submit(client, patient.user, LOW_ANSWERS).json()
    tampered = submit(client, patient.user, [{**a, "score": 99} for a in LOW_ANSWERS]).json()
    assert tampered["result"] == honest["result"] and tampered["total_score"] == honest["total_score"]


def test_only_a_patient_may_submit(client, db):
    caregiver = make_caregiver(db)
    assert submit(client, caregiver.user, LOW_ANSWERS).status_code in (401, 403)
    assert client.post(f"{API}/submit", json={"answers": []}).status_code in (401, 403)
    assert client.get(f"{API}/config").status_code in (401, 403)


# --- SR-5 and the rate limit ---------------------------------------------------------------------------------------------------

def test_if_scoring_fails_no_tier_is_stored_or_returned(db, client, patient, monkeypatch):
    def boom(_payload):
        raise RuntimeError("PRIVATE-ANSWER in the message")

    monkeypatch.setattr(scoring, "score_assessment", boom)
    r = submit(client, patient.user, LOW_ANSWERS)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "SCORING_UNAVAILABLE" and "PRIVATE-ANSWER" not in r.text
    assert r.json()["detail"]["message"].startswith("We could not calculate your result.")
    assert db.query(FallGuardAssessment).count() == 0


def test_if_the_result_cannot_be_stored_the_answer_is_503_and_nothing_is_kept(db, client, patient, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(fallguard_service.recommendations, "applicable", boom)
    r = submit(client, patient.user, LOW_ANSWERS)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SCORING_UNAVAILABLE"
    assert db.query(FallGuardAssessment).count() == 0


def test_the_eleventh_submission_in_an_hour_is_a_429(db, client, patient):
    for _ in range(fallguard_service.SUBMIT_RATE_LIMIT_PER_HOUR):
        assert submit(client, patient.user, LOW_ANSWERS).status_code == 200
    r = submit(client, patient.user, LOW_ANSWERS)
    assert r.status_code == 429 and code(r) == "fallguard_rate_limited"
    assert db.query(FallGuardAssessment).count() == fallguard_service.SUBMIT_RATE_LIMIT_PER_HOUR


# --- the optional AI summary: off by default, flagged for review when on ---------------------------------------------------------

def test_the_ai_summary_is_off_by_default_and_nothing_is_sent(client, patient, gemini):
    r = submit(client, patient.user, APPENDIX_A_ANSWERS)
    assert r.status_code == 200 and r.json()["ai_summary"] is None and gemini.calls == []


def test_when_switched_on_the_ai_summary_is_flagged_for_clinician_review_and_never_gets_free_text(client, patient, gemini, monkeypatch):
    monkeypatch.setattr(settings, "FALLGUARD_AI_ADVICE_ENABLED", True)
    r = submit(client, patient.user, [*APPENDIX_A_ANSWERS, {"question_id": "C7", "answer": ["Placeholder herbal tea"]}])
    body = r.json()
    assert r.status_code == 200 and body["ai_summary"] == "fake summary" and body["ai_review_status"] == "PENDING_CLINICIAN_REVIEW"
    sent, score = gemini.calls[0]
    assert score == 18 and "C7" not in {a["question_id"] for a in sent}
    assert "Placeholder herbal tea" not in str(sent) and "1946" not in str(sent)  # no free text, no date of birth


def test_an_ai_service_that_raises_never_fails_the_submission(client, patient, monkeypatch):
    class Boom:
        async def analyze_fallguard_assessment(self, answers, total_score):
            raise RuntimeError("PRIVATE-ANSWER leaked in the message")

    monkeypatch.setattr(settings, "FALLGUARD_AI_ADVICE_ENABLED", True)
    monkeypatch.setattr(fallguard_service, "gemini_service", Boom())
    r = submit(client, patient.user, APPENDIX_A_ANSWERS)
    assert r.status_code == 200 and r.json()["ai_summary"] is None and r.json()["result"]["final_tier"] == "HIGH"


# --- the questionnaire and config ------------------------------------------------------------------------------------------------

def test_questions_list_every_item_in_order_in_the_older_shape_plus_additive_fields(client, patient):
    items = client.get(f"{API}/questions", headers=auth_headers(patient.user)).json()
    assert [i["id"] for i in items] == scoring.ITEM_ORDER and [i["order"] for i in items] == list(range(1, 36))
    a1 = items[0]
    assert a1["options"] == [{"text": "no", "score": 0, "code": 0}, {"text": "once", "score": 1, "code": 1},
                             {"text": "twice or more", "score": 2, "code": 2}]
    assert a1["required"] is True and a1["part"] == "A" and a1["wording_status"] == "PENDING_CLINICAL_WORDING"
    assert next(i for i in items if i["id"] == "A2")["show_if"] == {"item": "A1", "op": ">=", "value": 1}


def test_config_has_versions_items_red_flags_messages_and_the_emergency_number(client, patient):
    r = client.get(f"{API}/config?lang=yo", headers=auth_headers(patient.user))
    cfg = r.json()
    assert r.status_code == 200 and cfg["language"] == "en"  # only English exists; the app falls back per string
    assert (cfg["questionnaire_version"], cfg["algorithm_version"], cfg["content_version"]) == ("2.0.0", "2.0.0", "2.0.0")
    assert cfg["emergency_number"] == "112" and cfg["min_age"] == 60 and cfg["assessment_minutes"] == 8
    assert [r["id"] for r in cfg["red_flags"]] == [f"R{n}" for n in range(1, 8)] and len(cfg["items"]) == 35
    assert cfg["messages"]["urgent.message"].startswith("Please get medical help now.")
    assert cfg["content_sign_off_status"] == "PENDING_CLINICAL_LEAD_SIGN_OFF"


def test_the_admin_question_editor_is_retired_but_the_list_is_readable(client, db):
    h = auth_headers(make_admin(db, ["super_admin"], password=None))
    assert client.get(f"{API}/admin/questions", headers=h).status_code == 200
    for method, path in (("POST", "admin/question"), ("PUT", f"admin/question/{uuid.uuid4()}"),
                         ("DELETE", f"admin/question/{uuid.uuid4()}")):
        r = client.request(method, f"{API}/{path}", headers=h)
        assert r.status_code == 410 and r.json()["detail"]["code"] == "fallguard_questionnaire_is_versioned"
