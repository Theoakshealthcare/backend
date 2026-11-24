"""Caregiver "I apply as": the requested roles in setup step 2, their form options and the admin's view of them.

The request grants nothing: skills stay admin-confirmed and approval still needs one.
"""

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.db.models.caregiver import Caregiver, CaregiverSkill
from app.tests.factories import auth_headers, make_admin, make_caregiver
from app.tests.test_caregiver_approval import BASE, make_doc, make_ready  # noqa: F401

STEP2 = "/api/v1/caregiver/profile/step2"
PROFILE = "/api/v1/caregiver/profile"
BODY = {"specialization": "Elderly Care", "years_of_experience": 3, "preferred_work_hours": "Flexible Hours",
        "service_areas": ["Personal Care"]}


def new_caregiver(db, **overrides):
    """A caregiver who has not passed step 2 yet (a first submission)."""
    return make_caregiver(db, approved=False, profile_completion_step=1, specialization=None,
                          years_of_experience=None, **overrides)


@pytest.fixture
def admin(db):
    return make_admin(db)


# --- caregiver step 2 ---------------------------------------------------------------


def test_step2_stores_and_returns_the_requested_roles(db, client):
    cg = new_caregiver(db)
    r = client.put(STEP2, json={**BODY, "applying_as": ["nurse", "caregiver"]}, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    assert r.json()["applying_as"] == ["nurse", "caregiver"]
    db.refresh(cg)
    assert cg.applying_as == ["nurse", "caregiver"] and cg.profile_completion_step == 2
    # resume/edit: the profile GET shows the saved value
    again = client.get(PROFILE, headers=auth_headers(cg.user))
    assert again.status_code == 200 and again.json()["applying_as"] == ["nurse", "caregiver"]


def test_step2_never_grants_a_skill(db, client):
    cg = new_caregiver(db)
    client.put(STEP2, json={**BODY, "applying_as": ["doctor", "nurse"]}, headers=auth_headers(cg.user))
    assert db.query(CaregiverSkill).filter_by(caregiver_id=cg.id).count() == 0


def test_a_first_submission_without_applying_as_is_a_422_and_saves_nothing(db, client):
    cg = new_caregiver(db)
    r = client.put(STEP2, json=BODY, headers=auth_headers(cg.user))
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "applying_as"] and r.json()["detail"][0]["type"] == "missing"
    db.refresh(cg)
    assert cg.profile_completion_step == 1 and cg.applying_as is None and cg.specialization is None


@pytest.mark.parametrize("bad", [
    ["surgeon"],
    ["nurse", "nurse"],
    [],
    None,
    "nurse",
    ["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist", "nurse"],
], ids=["unknown", "duplicate", "empty", "null", "not-a-list", "six"])
def test_bad_applying_as_values_are_422(db, client, bad):
    cg = new_caregiver(db)
    r = client.put(STEP2, json={**BODY, "applying_as": bad}, headers=auth_headers(cg.user))
    assert r.status_code == 422
    db.refresh(cg)
    assert cg.applying_as is None and cg.profile_completion_step == 1


def test_all_five_roles_are_accepted(db, client):
    cg = new_caregiver(db)
    roles = ["nurse", "doctor", "physiotherapist", "caregiver", "nutritionist"]
    r = client.put(STEP2, json={**BODY, "applying_as": roles}, headers=auth_headers(cg.user))
    assert r.status_code == 200 and r.json()["applying_as"] == roles


def test_a_legacy_row_may_resave_step2_without_it_and_stays_null(db, client):
    legacy = make_caregiver(db, approved=False, profile_completion_step=3)  # passed step 2 before the field existed
    assert legacy.applying_as is None
    r = client.put(STEP2, json=BODY, headers=auth_headers(legacy.user))
    assert r.status_code == 200 and r.json()["applying_as"] is None
    db.refresh(legacy)
    assert legacy.applying_as is None and legacy.profile_completion_step == 3


def test_a_resave_without_applying_as_keeps_the_stored_roles_and_a_resave_with_it_replaces_them(db, client):
    cg = make_caregiver(db, approved=False, profile_completion_step=2, applying_as=["nurse"])
    h = auth_headers(cg.user)
    assert client.put(STEP2, json=BODY, headers=h).status_code == 200
    db.refresh(cg)
    assert cg.applying_as == ["nurse"]
    assert client.put(STEP2, json={**BODY, "applying_as": ["physiotherapist"]}, headers=h).status_code == 200
    db.refresh(cg)
    assert cg.applying_as == ["physiotherapist"]


def test_a_row_without_roles_that_is_still_before_step_2_must_send_them_even_if_it_has_other_fields(db, client):
    cg = new_caregiver(db, applying_as=None)
    assert client.put(STEP2, json=BODY, headers=auth_headers(cg.user)).status_code == 422


def test_the_database_rejects_a_role_outside_the_five(db):
    cg = make_caregiver(db, approved=False)
    with pytest.raises(IntegrityError):
        db.execute(update(Caregiver).where(Caregiver.id == cg.id).values(applying_as=["surgeon"]))
        db.commit()
    db.rollback()


# --- form options -------------------------------------------------------------------


def test_form_options_lists_the_five_roles_with_labels(client):
    body = client.get("/api/v1/caregiver/form-options").json()
    assert body["roles"] == [
        {"value": "nurse", "label": "Nurse"},
        {"value": "doctor", "label": "Doctor"},
        {"value": "physiotherapist", "label": "Physiotherapist"},
        {"value": "caregiver", "label": "Caregiver"},
        {"value": "nutritionist", "label": "Nutritionist"},
    ]
    assert {"specializations", "service_areas", "work_hours", "states"} <= set(body)  # the rest is unchanged


# --- admin console ------------------------------------------------------------------


def test_admin_detail_returns_the_requested_roles(db, client, admin):
    cg = make_ready(db, applying_as=["caregiver", "nurse"])
    body = client.get(f"{BASE}/{cg.id}", headers=auth_headers(admin)).json()
    assert body["requested_skills"] == ["caregiver", "nurse"]
    assert body["profile"]["applying_as"] == ["caregiver", "nurse"]
    assert body["skills"] == [{"service_type": "nurse", "sub_specialty": None}]  # confirmed skills are separate


def test_admin_detail_requested_roles_is_an_empty_list_for_a_legacy_row(db, client, admin):
    cg = make_ready(db)
    body = client.get(f"{BASE}/{cg.id}", headers=auth_headers(admin)).json()
    assert body["requested_skills"] == [] and body["profile"]["applying_as"] is None


def test_requested_roles_do_not_make_a_caregiver_approvable(db, client, admin):
    cg = make_ready(db, applying_as=["nurse"])
    db.query(CaregiverSkill).filter_by(caregiver_id=cg.id).delete()
    db.commit()
    h = auth_headers(admin)
    r = client.post(f"{BASE}/{cg.id}/approve", headers=h)
    assert r.status_code == 409
    (unmet,) = r.json()["detail"]["unmet"]
    assert unmet["code"] == "NO_SKILLS"
    assert "the caregiver requested: Nurse" in unmet["message"]
    db.refresh(cg)
    assert cg.profile_status == "under_review"
    assert db.query(CaregiverSkill).filter_by(caregiver_id=cg.id).count() == 0

    # only the admin's own skills call grants one, and then approval goes through
    assert client.put(f"{BASE}/{cg.id}/skills", json={"skills": [{"service_type": "nurse"}]}, headers=h).status_code == 200
    assert client.post(f"{BASE}/{cg.id}/approve", headers=h).status_code == 200


def test_the_no_skills_message_is_unchanged_without_a_request(db, client, admin):
    cg = make_ready(db)
    db.query(CaregiverSkill).filter_by(caregiver_id=cg.id).delete()
    db.commit()
    r = client.post(f"{BASE}/{cg.id}/approve", headers=auth_headers(admin))
    (unmet,) = r.json()["detail"]["unmet"]
    assert unmet == {"code": "NO_SKILLS", "message": "At least one service skill must be set"}
