"""POST /fallguard/submit: the server scores the assessment from the stored options.

The patient app used to send each answer's score and the server summed them (a client could keep a 99). Now the score is
looked up from the stored option whose text equals the answer; the client's ``score`` is accepted and ignored. Bad
submissions are a 422 with a stable ``detail.code``, never a 500. The Gemini summary is optional: it is faked here.
"""

import json
import uuid

import pytest

from app.core.security import decrypt
from app.db.models.fallguard import FallGuardAnswer, FallGuardAssessment
from app.services import fallguard as fallguard_service
from app.services.fallguard import FallGuardService
from app.tests.factories import auth_headers, make_caregiver, make_patient

API = "/api/v1/fallguard"

QUESTIONS = [
    {"question": "Age?", "order": 1, "options": [
        {"text": "Less than 65", "score": 0}, {"text": "65–74", "score": 1}, {"text": "85+", "score": 3}]},
    {"question": "Fallen?", "order": 2, "options": [
        {"text": "No", "score": 0}, {"text": "Yes, 3+ times", "score": 2}]},
    {"question": "Alcohol?", "order": 3, "options": [
        {"text": "Never", "score": 1}, {"text": "Daily or almost daily", "score": 0}]},  # the seed's reversed Q24 shape
]


class FakeGemini:
    def __init__(self):
        self.calls = []

    async def analyze_fallguard_assessment(self, answers, total_score):
        self.calls.append((answers, total_score))
        return {"summary": "fake summary", "recommendations": ["fake advice"]}


@pytest.fixture(autouse=True)
def gemini(monkeypatch):
    fake = FakeGemini()
    monkeypatch.setattr(fallguard_service, "gemini_service", fake)
    return fake


@pytest.fixture
def patient(db):
    return make_patient(db)


@pytest.fixture
def qs(db):
    service = FallGuardService(db)
    return [service.create_question(q) for q in QUESTIONS]


def submit(client, user, answers):
    return client.post(f"{API}/submit", json={"answers": answers}, headers=auth_headers(user))


def code(response):
    return response.json()["detail"]["code"]


def test_the_total_is_the_sum_of_the_stored_option_scores(client, patient, qs):
    r = submit(client, patient.user, [
        {"question_id": str(qs[0].id), "answer": "85+", "score": 3},
        {"question_id": str(qs[1].id), "answer": "Yes, 3+ times", "score": 2},
        {"question_id": str(qs[2].id), "answer": "Never", "score": 1},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_score"] == 6
    assert [(a["answer"], a["score"]) for a in body["answers"]] == [("85+", 3), ("Yes, 3+ times", 2), ("Never", 1)]
    assert body["ai_summary"] == "fake summary" and body["ai_recommendations"] == ["fake advice"]


def test_a_score_the_client_makes_up_is_ignored_in_the_total_and_the_stored_answer(db, client, patient, qs, gemini):
    r = submit(client, patient.user, [{"question_id": str(qs[0].id), "answer": "Less than 65", "score": 99}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_score"] == 0 and body["answers"][0]["score"] == 0
    stored = db.query(FallGuardAnswer).one()
    assert stored.score == 0 and decrypt(stored.answer_encrypted) == "Less than 65"
    assert db.query(FallGuardAssessment).one().total_score == 0
    assert gemini.calls[0][1] == 0 and gemini.calls[0][0][0]["score"] == 0  # the summary is told the server's score


def test_a_negative_or_huge_client_score_cannot_move_the_total(client, patient, qs):
    r = submit(client, patient.user, [
        {"question_id": str(qs[0].id), "answer": "65–74", "score": -50},
        {"question_id": str(qs[1].id), "answer": "No", "score": 10**9},
    ])
    assert r.status_code == 200 and r.json()["total_score"] == 1


def test_the_score_field_is_optional(client, patient, qs):
    r = submit(client, patient.user, [{"question_id": str(qs[0].id), "answer": "65–74"}])
    assert r.status_code == 200 and r.json()["total_score"] == 1


def test_an_empty_answer_list_is_a_422_and_stores_nothing(db, client, patient, qs):
    r = submit(client, patient.user, [])
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "fallguard_no_answers", "message": "Answer at least one question."}
    assert db.query(FallGuardAssessment).count() == 0


def test_the_same_question_twice_is_a_422(db, client, patient, qs):
    r = submit(client, patient.user, [
        {"question_id": str(qs[0].id), "answer": "Less than 65"},
        {"question_id": str(qs[0].id), "answer": "85+"},
    ])
    assert r.status_code == 422 and code(r) == "fallguard_duplicate_question"
    assert r.json()["detail"]["question_id"] == str(qs[0].id)
    assert db.query(FallGuardAssessment).count() == 0


def test_an_unknown_question_is_a_422_not_a_500(db, client, patient, qs):
    r = submit(client, patient.user, [{"question_id": str(uuid.uuid4()), "answer": "No", "score": 0}])
    assert r.status_code == 422 and code(r) == "fallguard_unknown_question"
    assert db.query(FallGuardAssessment).count() == 0


def test_an_inactive_question_counts_as_unknown(db, client, patient, qs):
    qs[0].is_active = False
    db.commit()
    r = submit(client, patient.user, [{"question_id": str(qs[0].id), "answer": "Less than 65"}])
    assert r.status_code == 422 and code(r) == "fallguard_unknown_question"


@pytest.mark.parametrize("answer", ["Maybe", "", "less than 65", "65-74"])  # the last uses a hyphen, not the stored en dash
def test_an_answer_that_is_not_an_option_is_a_422(db, client, patient, qs, answer):
    r = submit(client, patient.user, [{"question_id": str(qs[0].id), "answer": answer, "score": 0}])
    assert r.status_code == 422 and code(r) == "fallguard_invalid_option"
    assert r.json()["detail"]["question_id"] == str(qs[0].id)
    assert db.query(FallGuardAssessment).count() == 0


def test_one_bad_answer_rejects_the_whole_submission(db, client, patient, qs):
    r = submit(client, patient.user, [
        {"question_id": str(qs[0].id), "answer": "Less than 65"},
        {"question_id": str(qs[1].id), "answer": "Sometimes"},
    ])
    assert r.status_code == 422
    assert db.query(FallGuardAssessment).count() == 0 and db.query(FallGuardAnswer).count() == 0


def test_surrounding_whitespace_in_the_answer_is_tolerated_and_the_stored_text_is_canonical(db, client, patient, qs):
    r = submit(client, patient.user, [{"question_id": str(qs[1].id), "answer": " Yes, 3+ times "}])
    assert r.status_code == 200 and r.json()["answers"][0]["answer"] == "Yes, 3+ times"
    assert r.json()["total_score"] == 2


def test_malformed_bodies_stay_a_pydantic_422(client, patient, qs):
    r = client.post(f"{API}/submit", json={"answers": [{"answer": "No"}]}, headers=auth_headers(patient.user))
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_history_returns_the_server_computed_result(client, patient, qs):
    submit(client, patient.user, [{"question_id": str(qs[0].id), "answer": "Less than 65", "score": 99}])
    history = client.get(f"{API}/history", headers=auth_headers(patient.user)).json()
    assert [h["total_score"] for h in history] == [0]


def test_the_summary_failing_still_stores_the_score(client, patient, qs, monkeypatch):
    class Down:
        async def analyze_fallguard_assessment(self, answers, total_score):
            return {"summary": "Unable to analyze", "recommendations": []}  # what the real service returns without a key

    monkeypatch.setattr(fallguard_service, "gemini_service", Down())
    r = submit(client, patient.user, [{"question_id": str(qs[0].id), "answer": "85+"}])
    assert r.status_code == 200
    assert r.json()["total_score"] == 3 and r.json()["ai_summary"] == "Unable to analyze"


def test_only_a_patient_may_submit(client, db, qs):
    caregiver = make_caregiver(db)
    r = submit(client, caregiver.user, [{"question_id": str(qs[0].id), "answer": "85+"}])
    assert r.status_code in (401, 403)
    assert client.post(f"{API}/submit", json={"answers": []}).status_code in (401, 403)


def test_seeded_options_are_what_the_server_scores_with(db, client, patient):
    """The real seeded questionnaire: every option of every question round-trips to its seeded value."""
    from app.scripts.seed_fallguard_questions import QUESTIONS as SEED

    service = FallGuardService(db)
    created = [(service.create_question(q), q) for q in SEED]
    for question, seed in created:
        for option in seed["options"]:
            r = submit(client, patient.user, [{"question_id": str(question.id), "answer": option["text"], "score": 99}])
            assert r.status_code == 200, (seed["question"], option, r.text)
            assert r.json()["total_score"] == option["score"], (seed["question"], option)
    assert json.loads(created[0][0].options)  # sanity: stored as JSON text
