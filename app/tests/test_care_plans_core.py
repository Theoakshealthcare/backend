"""Care plans, slice B1: create, read, edit, goals, templates, the lists and the visit summary.

Access (who may do what, and the one identical 404) is in test_care_plans_access.py; encryption, erasure and the
family-view groundwork are in test_care_plans_security.py.
"""

import json
import threading
import pytest

from app.db.models.booking import BookingStatus
from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanGoal, CarePlanTask, CarePlanTemplate
from app.db.models.patient import Patient
from app.services.care_plans import local_today
from app.tests.care_plan_helpers import (
    BASE, SECRET_ALLERGY, SECRET_DIAGNOSIS, SECRET_GOAL, SECRET_INSTRUCTION, SECRET_TARGET, SECRET_TASK, SECRET_TEXTS,
    a_template, create_plan, h, make_nurse, plan_body, relative_visit, self_visit,
)
from app.tests.factories import make_booking, make_caregiver, make_patient


def _plan_url(plan_id):
    return f"{BASE}/care-plans/{plan_id}"


def _created(client, db, **over):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    r = create_plan(client, nurse, booking, **over)
    assert r.status_code == 201, r.text
    return nurse, patient, booking, r.json()


# --- create ---------------------------------------------------------------------------------------------------------

def test_a_nurse_creates_a_plan_for_a_client_booked_for_themselves(db, client):
    nurse, patient, booking, plan = _created(client, db)
    assert plan["status"] == "active" and plan["version"] == 1
    assert plan["booking_id"] == str(booking.id)
    assert plan["client"] == {"full_name": "Placeholder Client", "age": 71, "gender": "female"}
    assert plan["diagnosis"] == SECRET_DIAGNOSIS and plan["risk_level"] == "high"
    assert plan["start_date"] == "2026-10-28" and plan["review_due_on"] == "2026-11-11"
    assert plan["allergies"] == [SECRET_ALLERGY] and plan["special_instructions"] == [SECRET_INSTRUCTION]
    assert [g["title"] for g in plan["goals"]] == [SECRET_GOAL] and plan["goals"][0]["target"] == SECRET_TARGET
    assert plan["tasks"][0]["title"] == SECRET_TASK and plan["tasks"][0]["time_of_day"] == "08:00"
    assert plan["tasks"][0]["days_of_week"] == [1, 3, 5]
    assert plan["author"] == {"name": nurse.full_name, "kind": "caregiver"}
    assert plan["template"] is None and plan["capabilities"] == {"can_edit_plan": True, "can_tick_tasks": True, "can_add_observation": True}
    row = db.get(CarePlan, plan["id"])
    assert row.patient_id == patient.id and row.care_recipient_id is None
    assert row.created_by_caregiver_id == nurse.id and row.created_from_booking_id == booking.id


def test_the_plan_of_a_visit_booked_for_a_relative_belongs_to_the_recipient(db, client):
    nurse = make_nurse(db)
    booker, recipient, booking = relative_visit(db, nurse)
    r = create_plan(client, nurse, booking)
    assert r.status_code == 201, r.text
    assert r.json()["client"]["full_name"] == "Placeholder Relative"
    row = db.get(CarePlan, r.json()["id"])
    assert row.care_recipient_id == recipient.id and row.patient_id is None


def test_an_app_booking_for_someone_else_has_no_identifiable_client(db, client):
    nurse = make_nurse(db)
    booker = make_patient(db)
    booking = make_booking(db, booker, nurse, status=BookingStatus.IN_PROGRESS, booking_for="mother")
    summary = client.get(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse))
    assert summary.status_code == 200
    assert summary.json() == {"booking_id": str(booking.id), "subject_ready": False, "can_author": False, "plan": None}
    r = create_plan(client, nurse, booking)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "client_not_identified"


def test_creating_from_a_template_copies_its_content_and_records_where_it_came_from(db, client):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    template = a_template(db)
    r = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse),
                    json={"template_id": str(template.id), "diagnosis": SECRET_DIAGNOSIS})
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["risk_level"] == "high" and plan["duration_days"] == 30 and plan["review_every_days"] == 14
    assert plan["template"] == {"id": str(template.id), "name": "Stroke Recovery", "version": 1}
    assert len(plan["goals"]) == len(template.body["goals"]) and len(plan["tasks"]) == len(template.body["tasks"])
    assert plan["special_instructions"] == template.body["special_instructions"]
    assert plan["start_date"] == local_today().isoformat()  # today in Africa/Lagos
    actions = [e.action for e in db.query(CarePlanEvent).filter_by(plan_id=plan["id"])]
    assert sorted(actions) == ["plan.create", "template.use"]


def test_a_plan_is_a_copy_of_the_template_not_a_reference(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    template = a_template(db)
    plan = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse),
                       json={"template_id": str(template.id), "diagnosis": SECRET_DIAGNOSIS}).json()
    before = [g["title"] for g in plan["goals"]]
    template.body = {**template.body, "goals": [{"title": "Changed later", "target": None, "frequency": "daily"}]}
    template.version = 2
    db.commit()  # the harness restores the seeded templates after every test
    again = client.get(_plan_url(plan["id"]), headers=h(nurse)).json()
    assert [g["title"] for g in again["goals"]] == before
    assert again["template"]["version"] == 1


def test_what_the_body_sends_wins_over_the_template(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    r = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse), json=plan_body(
        template_id=str(a_template(db).id), risk_level="low", duration_days=10, goals=[], tasks=[]))
    plan = r.json()
    assert plan["risk_level"] == "low" and plan["duration_days"] == 10
    assert plan["goals"] == [] and plan["tasks"] == []


def test_an_unknown_template_is_a_404_with_its_own_code(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    r = client.post(f"{BASE}/bookings/{booking.id}/care-plan", headers=h(nurse), json=plan_body(
        template_id="00000000-0000-4000-8000-000000000009"))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "template_not_found"


def test_a_blank_plan_needs_a_risk_level(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    body = plan_body()
    del body["risk_level"]
    r = client.post(f"{BASE}/bookings/{booking.id}/care-plan", json=body, headers=h(nurse))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_care_plan"
    assert "risk_level" in r.json()["detail"]["message"]


def test_one_active_plan_per_client_and_the_error_names_it(db, client):
    nurse, patient, booking, plan = _created(client, db)
    r = create_plan(client, nurse, booking)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "care_plan_exists", "message": "This client already has an active care plan.",
                                  "plan_id": plan["id"]}


def test_two_creates_at_once_leave_one_plan(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    results = []

    def go():
        results.append(create_plan(client, nurse, booking).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201, 409]
    assert db.query(CarePlan).count() == 1


@pytest.mark.parametrize("over", [
    {"diagnosis": ""}, {"diagnosis": "x" * 201}, {"risk_level": "extreme"}, {"duration_days": 0}, {"duration_days": 366},
    {"review_every_days": 6}, {"review_every_days": 91},
    {"goals": [{"title": "", "frequency": "daily"}]}, {"goals": [{"title": "x" * 121, "frequency": "daily"}]},
    {"goals": [{"title": "g", "frequency": "hourly"}]},
    {"goals": [{"title": "g", "frequency": "daily"}] * 31},
    {"tasks": [{"title": "t", "time_slot": "noon", "days_of_week": [1]}]},
    {"tasks": [{"title": "t", "time_slot": "morning", "days_of_week": []}]},
    {"tasks": [{"title": "t", "time_slot": "morning", "days_of_week": [0]}]},
    {"tasks": [{"title": "t", "time_slot": "morning", "days_of_week": [1, 1]}]},
    {"tasks": [{"title": "t", "time_slot": "morning", "time_of_day": "25:00", "days_of_week": [1]}]},
    {"tasks": [{"title": "t", "time_slot": "morning", "days_of_week": [1]}] * 61},
    {"allergies": ["x" * 1001]}, {"special_instructions": [""]},
])
def test_field_limits_are_422_and_never_echo_the_value(db, client, over):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    r = create_plan(client, nurse, booking, **over)
    assert r.status_code == 422, r.text
    assert db.query(CarePlan).count() == 0
    assert all("input" not in e for e in r.json()["detail"])


def test_a_422_never_echoes_the_rejected_health_text(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    too_long = "Placeholder secret diagnosis " * 20
    r = create_plan(client, nurse, booking, diagnosis=too_long, allergies=["x" * 1001], goals=[{"title": too_long, "frequency": "x"}])
    assert r.status_code == 422
    assert "Placeholder secret" not in r.text and "xxxxxxxx" not in r.text
    assert {tuple(e["loc"]) for e in r.json()["detail"]} >= {("body", "diagnosis"), ("body", "allergies", 0)}


def test_a_model_level_422_does_not_echo_the_whole_body(db, client):
    nurse, _, _, plan = _created(client, db)
    r = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={
        "expected_version": plan["version"], "risk_level": None, "allergies": ["Placeholder penicillin"]})
    assert r.status_code == 422 and "Placeholder penicillin" not in r.text and "input" not in r.text


def test_reactivating_a_discontinued_goal_respects_the_goal_limit(db, client):
    nurse, _, _, plan = _created(client, db, goals=[{"title": f"g{i}", "frequency": "daily"} for i in range(30)])
    first = plan["goals"][0]["id"]
    gone = client.delete(f"{BASE}/care-plan-goals/{first}?expected_version={plan['version']}", headers=h(nurse)).json()
    extra = client.post(f"{_plan_url(plan['id'])}/goals", headers=h(nurse), json={"title": "x", "frequency": "daily"}).json()
    back = client.patch(f"{BASE}/care-plan-goals/{first}", headers=h(nurse),
                        json={"expected_version": extra["plan_version"], "status": "active"})
    assert back.status_code == 422 and back.json()["detail"]["code"] == "invalid_care_plan"
    assert gone["goal"]["status"] == "discontinued"


def test_the_assigned_list_search_matches_every_plan_before_the_cap_is_applied(db, client):
    nurse = make_nurse(db)
    for _ in range(3):
        _, booking = self_visit(db, nurse)
        create_plan(client, nurse, booking)
    assert len(client.get(f"{BASE}/care-plans?q=placeholder", headers=h(nurse)).json()["items"]) == 3


# --- read -----------------------------------------------------------------------------------------------------------

def test_get_returns_the_same_plan_and_never_caches_it(db, client):
    nurse, _, _, plan = _created(client, db)
    r = client.get(_plan_url(plan["id"]), headers=h(nurse))
    assert r.status_code == 200 and r.json() == plan
    assert r.headers["cache-control"] == "no-store"


def test_a_response_is_a_closed_list_with_no_ciphertext_and_no_family_flag(db, client):
    nurse, _, _, plan = _created(client, db)
    assert set(plan) == {"id", "version", "status", "booking_id", "client", "diagnosis", "risk_level", "start_date",
                         "duration_days", "review_every_days", "review_due_on", "template", "author", "allergies",
                         "special_instructions", "goals", "tasks", "capabilities", "created_at", "updated_at"}
    assert set(plan["goals"][0]) == {"id", "title", "target", "frequency", "status"}
    assert set(plan["tasks"][0]) == {"id", "title", "time_slot", "time_of_day", "days_of_week"}
    assert "gAAAAA" not in json.dumps(plan) and "family_visible" not in json.dumps(plan)
    for secret in ("Secret Close", "ada.secret", "+234"):
        assert secret not in json.dumps(plan)


def test_a_non_nurse_reads_the_plan_but_cannot_edit(db, client):
    nurse, patient, booking, plan = _created(client, db)
    other = make_caregiver(db)  # specialization says "nurse" but there is no skill row
    make_booking(db, patient, other, status=BookingStatus.ACCEPTED, booking_for="myself")
    r = client.get(_plan_url(plan["id"]), headers=h(other))
    assert r.status_code == 200 and r.json()["capabilities"] == {"can_edit_plan": False, "can_tick_tasks": False, "can_add_observation": False}  # accepted, not started
    assert r.json()["diagnosis"] == SECRET_DIAGNOSIS


def test_the_plan_follows_the_client_to_the_next_caregiver(db, client):
    nurse, patient, booking, plan = _created(client, db)
    successor = make_nurse(db)
    nxt = make_booking(db, patient, successor, status=BookingStatus.ACCEPTED, booking_for="myself")
    r = client.get(_plan_url(plan["id"]), headers=h(successor))
    assert r.status_code == 200 and r.json()["booking_id"] == str(nxt.id)
    assert r.json()["author"]["name"] == nurse.full_name


# --- edit -----------------------------------------------------------------------------------------------------------

def test_patch_changes_only_what_is_sent_and_bumps_the_version(db, client):
    nurse, _, _, plan = _created(client, db)
    r = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={
        "expected_version": plan["version"], "risk_level": "low", "allergies": ["Placeholder allergen two"],
        "review_every_days": None})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["version"] == plan["version"] + 1 and new["risk_level"] == "low"
    assert new["allergies"] == ["Placeholder allergen two"] and new["review_every_days"] is None
    assert new["review_due_on"] is None
    assert new["diagnosis"] == plan["diagnosis"] and new["special_instructions"] == plan["special_instructions"]
    assert new["goals"] == plan["goals"] and new["tasks"] == plan["tasks"]


def test_clearing_allergies_and_special_instructions(db, client):
    nurse, _, _, plan = _created(client, db)
    r = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={
        "expected_version": plan["version"], "allergies": None, "special_instructions": []})
    assert r.status_code == 200 and r.json()["allergies"] == [] and r.json()["special_instructions"] == []
    row = db.get(CarePlan, plan["id"])
    db.refresh(row)
    assert row.allergies_encrypted is None and row.special_instructions_encrypted is None


@pytest.mark.parametrize("field", ["diagnosis", "risk_level", "start_date", "duration_days", "status"])
def test_required_fields_cannot_be_cleared(db, client, field):
    nurse, _, _, plan = _created(client, db)
    r = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"expected_version": plan["version"], field: None})
    assert r.status_code == 422


def test_a_stale_version_is_a_409_that_returns_the_current_plan(db, client):
    nurse, _, _, plan = _created(client, db)
    ok = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"expected_version": plan["version"], "risk_level": "low"})
    assert ok.status_code == 200
    stale = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"expected_version": plan["version"],
                                                                       "risk_level": "medium"})
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "version_conflict" and detail["plan"]["risk_level"] == "low"
    assert detail["plan"]["version"] == ok.json()["version"]
    assert db.get(CarePlan, plan["id"]).risk_level == "low"


def test_a_missing_expected_version_is_422(db, client):
    nurse, _, _, plan = _created(client, db)
    assert client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"risk_level": "low"}).status_code == 422


def test_completing_a_plan_makes_it_read_only_and_frees_the_client(db, client):
    nurse, patient, booking, plan = _created(client, db)
    done = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"expected_version": plan["version"], "status": "completed"})
    assert done.status_code == 200 and done.json()["status"] == "completed"
    assert done.json()["capabilities"] == {"can_edit_plan": False, "can_tick_tasks": False, "can_add_observation": False}
    row = db.get(CarePlan, plan["id"])
    db.refresh(row)
    assert row.completed_at is not None
    edit = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"expected_version": done.json()["version"], "risk_level": "low"})
    assert edit.status_code == 409 and edit.json()["detail"]["code"] == "plan_not_editable"
    goal = client.post(f"{_plan_url(plan['id'])}/goals", headers=h(nurse), json={"title": "g", "frequency": "daily"})
    assert goal.status_code == 409 and goal.json()["detail"]["code"] == "plan_not_editable"
    assert client.get(_plan_url(plan["id"]), headers=h(nurse)).status_code == 200  # still visible as history
    again = create_plan(client, nurse, booking, diagnosis="Placeholder second diagnosis")
    assert again.status_code == 201 and again.json()["id"] != plan["id"]


def test_archiving_frees_the_client_too(db, client):
    nurse, _, booking, plan = _created(client, db)
    r = client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"expected_version": plan["version"], "status": "archived"})
    assert r.status_code == 200 and r.json()["status"] == "archived"
    assert create_plan(client, nurse, booking).status_code == 201


def test_every_write_leaves_an_event_that_carries_no_health_text(db, client):
    nurse, _, _, plan = _created(client, db)
    client.patch(_plan_url(plan["id"]), headers=h(nurse), json={
        "expected_version": plan["version"], "diagnosis": "Placeholder diagnosis omega", "status": "completed"})
    events = db.query(CarePlanEvent).filter_by(plan_id=plan["id"]).order_by(CarePlanEvent.created_at).all()
    assert [e.action for e in events] == ["plan.create", "plan.update", "plan.status"]
    assert all(e.actor_type == "caregiver" and e.actor_id == nurse.id for e in events)
    assert events[1].detail == {"fields": ["diagnosis"]} and events[2].detail == {"from": "active", "to": "completed"}
    blob = json.dumps([e.detail for e in events])
    for secret in SECRET_TEXTS + ("omega",):
        assert secret not in blob


# --- goals ----------------------------------------------------------------------------------------------------------

def test_goals_add_edit_achieve_and_remove(db, client):
    nurse, _, _, plan = _created(client, db)
    added = client.post(f"{_plan_url(plan['id'])}/goals", headers=h(nurse),
                        json={"title": "Placeholder goal two", "target": "Placeholder target two", "frequency": "weekly"})
    assert added.status_code == 201, added.text
    goal, version = added.json()["goal"], added.json()["plan_version"]
    assert goal["title"] == "Placeholder goal two" and goal["status"] == "active" and version == plan["version"] + 1

    edited = client.patch(f"{BASE}/care-plan-goals/{goal['id']}", headers=h(nurse), json={
        "expected_version": version, "title": "Placeholder goal three", "target": None, "status": "achieved"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["goal"] == {**goal, "title": "Placeholder goal three", "target": None, "status": "achieved"}
    version = edited.json()["plan_version"]
    assert version == plan["version"] + 2

    removed = client.delete(f"{BASE}/care-plan-goals/{goal['id']}?expected_version={version}", headers=h(nurse))
    assert removed.status_code == 200 and removed.json()["goal"]["status"] == "discontinued"
    listed = client.get(_plan_url(plan["id"]), headers=h(nurse)).json()
    assert {g["id"]: g["status"] for g in listed["goals"]}[goal["id"]] == "discontinued"  # kept, with its status
    assert db.query(CarePlanGoal).filter_by(plan_id=plan["id"]).count() == 2  # never deleted


def test_goal_edits_need_the_current_version(db, client):
    nurse, _, _, plan = _created(client, db)
    goal_id = plan["goals"][0]["id"]
    stale = client.patch(f"{BASE}/care-plan-goals/{goal_id}", headers=h(nurse), json={"expected_version": 99, "title": "x"})
    assert stale.status_code == 409 and stale.json()["detail"]["plan"]["id"] == plan["id"]
    gone = client.delete(f"{BASE}/care-plan-goals/{goal_id}?expected_version=99", headers=h(nurse))
    assert gone.status_code == 409
    assert client.delete(f"{BASE}/care-plan-goals/{goal_id}", headers=h(nurse)).status_code == 422


def test_a_plan_holds_at_most_thirty_goals_that_are_not_discontinued(db, client):
    nurse, _, _, plan = _created(client, db, goals=[{"title": f"g{i}", "frequency": "daily"} for i in range(29)])
    url = f"{_plan_url(plan['id'])}/goals"
    assert client.post(url, headers=h(nurse), json={"title": "last", "frequency": "daily"}).status_code == 201
    over = client.post(url, headers=h(nurse), json={"title": "one too many", "frequency": "daily"})
    assert over.status_code == 422 and over.json()["detail"]["code"] == "invalid_care_plan"


# --- family_visible stays private ------------------------------------------------------------------------------------

def test_family_visible_defaults_false_and_no_request_can_set_it(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse)
    body = plan_body(goals=[{"title": "g", "frequency": "daily", "family_visible": True}],
                     tasks=[{"title": "t", "time_slot": "morning", "days_of_week": [1], "family_visible": True}])
    plan = client.post(f"{BASE}/bookings/{booking.id}/care-plan", json=body, headers=h(nurse)).json()
    added = client.post(f"{_plan_url(plan['id'])}/goals", headers=h(nurse),
                        json={"title": "g2", "frequency": "daily", "family_visible": True}).json()
    client.patch(f"{BASE}/care-plan-goals/{added['goal']['id']}", headers=h(nurse),
                 json={"expected_version": added["plan_version"], "family_visible": True, "title": "g3"})
    assert [g.family_visible for g in db.query(CarePlanGoal).filter_by(plan_id=plan["id"])] == [False, False]
    assert [t.family_visible for t in db.query(CarePlanTask).filter_by(plan_id=plan["id"])] == [False]


# --- templates, lists, the visit summary ---------------------------------------------------------------------------------

def test_a_nurse_lists_and_previews_the_five_seeded_templates(db, client):
    nurse = make_nurse(db)
    listed = client.get(f"{BASE}/care-plan-templates", headers=h(nurse))
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert sorted(i["key"] for i in items) == ["cardiac_rehabilitation", "diabetes_management",
                                               "elderly_daily_living_dementia", "post_surgery_recovery", "stroke_recovery"]
    assert all(i["clinical_review_status"] == "pending" for i in items)
    assert set(items[0]) == {"id", "key", "name", "summary", "duration_days", "review_every_days", "default_risk_level",
                             "clinical_review_status", "version"}
    detail = client.get(f"{BASE}/care-plan-templates/{items[0]['id']}", headers=h(nurse))
    assert detail.status_code == 200
    body = detail.json()
    assert body["goals"] and body["tasks"] and body["special_instructions"]
    assert set(body["tasks"][0]) == {"title", "time_slot", "time_of_day", "days_of_week"}


def test_templates_are_for_nurses_only(db, client):
    other = make_caregiver(db)  # self-declared "nurse" specialization is not the skill
    r = client.get(f"{BASE}/care-plan-templates", headers=h(other))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_nurse"
    assert client.get(f"{BASE}/care-plan-templates/00000000-0000-4000-8000-000000000009", headers=h(other)).status_code == 403
    nurse = make_nurse(db)
    assert client.get(f"{BASE}/care-plan-templates/00000000-0000-4000-8000-000000000009", headers=h(nurse)).status_code == 404


def test_seeded_templates_hold_no_medicines_doses_or_thresholds(db):
    blob = json.dumps([t.body for t in db.query(CarePlanTemplate)]).lower()
    for word in ("mg", " ml", "mmhg", "mmol", "units", "tablet", "dose of", "insulin"):
        assert word not in blob, word


def test_the_assigned_list_shows_plans_of_my_live_clients_and_searches_by_name(db, client):
    nurse, patient, booking, plan = _created(client, db)
    r = client.get(f"{BASE}/care-plans", headers=h(nurse))
    assert r.status_code == 200
    assert r.json()["items"] == [{
        "id": plan["id"], "booking_id": str(booking.id), "client": plan["client"], "diagnosis": SECRET_DIAGNOSIS,
        "risk_level": "high", "status": "active", "start_date": "2026-10-28"}]
    assert client.get(f"{BASE}/care-plans?q=placeholder cl", headers=h(nurse)).json()["items"][0]["id"] == plan["id"]
    assert client.get(f"{BASE}/care-plans?q=nobody", headers=h(nurse)).json()["items"] == []
    assert client.get(f"{BASE}/care-plans?status=completed", headers=h(nurse)).json()["items"] == []
    assert client.get(f"{BASE}/care-plans?status=archived", headers=h(nurse)).status_code == 422
    # another caregiver with no visit for this client sees nothing
    stranger = make_nurse(db)
    assert client.get(f"{BASE}/care-plans", headers=h(stranger)).json() == {"items": []}


def test_the_assigned_list_includes_a_relatives_plan_and_completed_history(db, client):
    nurse = make_nurse(db)
    _, _, booking = relative_visit(db, nurse)
    plan = create_plan(client, nurse, booking).json()
    items = client.get(f"{BASE}/care-plans", headers=h(nurse)).json()["items"]
    assert [i["client"]["full_name"] for i in items] == ["Placeholder Relative"]
    client.patch(_plan_url(plan["id"]), headers=h(nurse), json={"expected_version": plan["version"], "status": "completed"})
    assert client.get(f"{BASE}/care-plans", headers=h(nurse)).json()["items"] == []
    done = client.get(f"{BASE}/care-plans?status=completed", headers=h(nurse)).json()["items"]
    assert [i["id"] for i in done] == [plan["id"]]


def test_candidates_are_live_clients_without_an_active_plan(db, client):
    nurse = make_nurse(db)
    with_plan, plan_booking = self_visit(db, nurse)
    create_plan(client, nurse, plan_booking)
    without, open_booking = self_visit(db, nurse, status=BookingStatus.ACCEPTED)
    _, _, relative_booking = relative_visit(db, nurse, status=BookingStatus.EN_ROUTE)
    make_booking(db, make_patient(db), nurse, status=BookingStatus.IN_PROGRESS, booking_for="mother")  # unidentified
    r = client.get(f"{BASE}/care-plans/candidates", headers=h(nurse))
    assert r.status_code == 200, r.text
    got = {i["booking_id"] for i in r.json()["items"]}
    assert got == {str(open_booking.id), str(relative_booking.id)}
    assert set(r.json()["items"][0]) == {"booking_id", "client", "booking_status", "scheduled_date"}
    assert client.get(f"{BASE}/care-plans/candidates", headers=h(make_caregiver(db))).status_code == 403


def test_the_visit_summary_says_whether_a_plan_exists(db, client):
    nurse = make_nurse(db)
    _, booking = self_visit(db, nurse, status=BookingStatus.ACCEPTED)
    url = f"{BASE}/bookings/{booking.id}/care-plan"
    empty = client.get(url, headers=h(nurse))
    assert empty.status_code == 200
    assert empty.json() == {"booking_id": str(booking.id), "subject_ready": True, "can_author": True, "plan": None}
    plan = create_plan(client, nurse, booking).json()
    full = client.get(url, headers=h(nurse)).json()
    assert full["plan"] == {"id": plan["id"], "version": 1, "status": "active", "diagnosis": SECRET_DIAGNOSIS,
                            "risk_level": "high", "start_date": "2026-10-28", "duration_days": 30,
                            "review_every_days": 14, "review_due_on": "2026-11-11", "goal_count": 1, "task_count": 1}
    other = make_caregiver(db)
    cg_booking = make_booking(db, db.get(Patient, booking.patient_id),
                              other, status=BookingStatus.ACCEPTED, booking_for="myself")
    seen = client.get(f"{BASE}/bookings/{cg_booking.id}/care-plan", headers=h(other)).json()
    assert seen["can_author"] is False and seen["plan"]["id"] == plan["id"]


def test_the_home_screen_tells_a_nurse_from_everyone_else(db, client):
    assert client.get(f"{BASE}/home", headers=h(make_nurse(db))).json()["can_author_care_plans"] is True
    assert client.get(f"{BASE}/home", headers=h(make_caregiver(db))).json()["can_author_care_plans"] is False
