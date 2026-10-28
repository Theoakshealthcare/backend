"""Care plans, admin API (slice B4): the Clinical Reviewer lists, opens, creates, edits and annotates plans, and signs
templates off. Who may call which route is in test_admin_rbac.py; the audit trail and privacy are in
test_admin_care_plans_audit.py."""

from datetime import date, timedelta

import pytest

from app.db.models.care import CareRecipient
from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanTemplate
from app.tests.care_plan_helpers import (
    BASE, SECRET_DIAGNOSIS, SECRET_GOAL, SECRET_MED, SECRET_NOTE, SECRET_TASK, a_template, add_med, create_plan, h, med_body,
    plan_body, relative_visit, self_visit, make_nurse, daily_task,
)
from app.tests.factories import auth_headers, make_admin, make_patient

ADMIN = "/api/v1/admin"


@pytest.fixture
def reviewer(db):
    return make_admin(db, ["clinical_reviewer"], password=None, full_name="Placeholder Reviewer")


@pytest.fixture
def rh(reviewer):
    return auth_headers(reviewer)


def admin_plan(client, rh, patient=None, **over):
    body = {**plan_body(), **({"patient_id": str(patient.id)} if patient else {}), **over}
    return client.post(f"{ADMIN}/care-plans", json=body, headers=rh)


def a_patient(db, **over):
    return make_patient(db, full_name=over.pop("full_name", "Placeholder Client"), gender="female",
                        date_of_birth=date.today() - timedelta(days=71 * 366), **over)


@pytest.fixture
def plan(db, client, rh):
    patient = a_patient(db)
    r = admin_plan(client, rh, patient)
    assert r.status_code == 201, r.text
    return r.json()


# --- create ---------------------------------------------------------------------------------------------------------

def test_an_admin_creates_a_plan_for_a_patient(db, client, rh, reviewer):
    patient = a_patient(db)
    r = admin_plan(client, rh, patient)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject"] == {"kind": "patient", "id": str(patient.id)}
    assert body["client"] == {"full_name": "Placeholder Client", "age": 71, "gender": "female"}
    assert body["status"] == "active" and body["version"] == 1 and body["diagnosis"] == SECRET_DIAGNOSIS
    assert body["author"] == {"name": "Placeholder Reviewer", "kind": "admin"}
    assert [g["title"] for g in body["goals"]] == [SECRET_GOAL] and body["tasks"][0]["title"] == SECRET_TASK
    assert body["medications"] == [] and "booking_id" not in body and "capabilities" not in body
    row = db.get(CarePlan, body["id"])
    assert row.created_by_admin_id == reviewer.id and row.created_by_caregiver_id is None
    assert row.created_from_booking_id is None and row.patient_id == patient.id


def test_an_admin_creates_a_plan_for_a_care_recipient(db, client, rh):
    booker = make_patient(db, full_name="Placeholder Booker")
    recipient = CareRecipient(full_name="Placeholder Relative", gender="male", date_of_birth=date(1950, 3, 4),
                              created_by_user_id=booker.user_id)
    db.add(recipient)
    db.commit()
    r = client.post(f"{ADMIN}/care-plans", json={**plan_body(), "care_recipient_id": str(recipient.id)}, headers=rh)
    assert r.status_code == 201, r.text
    assert r.json()["subject"] == {"kind": "recipient", "id": str(recipient.id)}
    assert r.json()["client"]["full_name"] == "Placeholder Relative"


def test_a_plan_from_a_template_copies_it_and_the_body_wins(db, client, rh):
    template = a_template(db)
    patient = a_patient(db)
    r = client.post(f"{ADMIN}/care-plans", headers=rh, json={
        "patient_id": str(patient.id), "template_id": str(template.id), "diagnosis": SECRET_DIAGNOSIS, "duration_days": 10})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["template"] == {"id": str(template.id), "name": template.name, "version": template.version}
    assert body["duration_days"] == 10 and body["risk_level"] == template.default_risk_level
    assert len(body["goals"]) == len(template.body["goals"]) and len(body["tasks"]) == len(template.body["tasks"])


@pytest.mark.parametrize("client_fields", [{}, "both"], ids=["neither", "both"])
def test_the_client_is_exactly_one_of_two_ids(db, client, rh, client_fields):
    patient = a_patient(db)
    fields = {"patient_id": str(patient.id), "care_recipient_id": str(patient.id)} if client_fields == "both" else {}
    r = client.post(f"{ADMIN}/care-plans", json={**plan_body(), **fields}, headers=rh)
    assert r.status_code == 422
    assert "input" not in str(r.json()) and SECRET_DIAGNOSIS not in r.text


def test_an_unknown_or_erased_client_is_client_not_found(db, client, rh):
    erased = a_patient(db)
    erased.is_active = False
    db.commit()
    for fields in ({"patient_id": "00000000-0000-4000-8000-000000000001"}, {"patient_id": str(erased.id)},
                   {"care_recipient_id": "00000000-0000-4000-8000-000000000002"}):
        r = client.post(f"{ADMIN}/care-plans", json={**plan_body(), **fields}, headers=rh)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "client_not_found", r.text
    assert db.query(CarePlan).count() == 0


def test_a_care_recipient_erased_with_its_owner_gets_no_new_plan(db, client, rh):
    from app.services.account_erasure import ERASED_NAME

    booker = make_patient(db, full_name="Placeholder Booker")
    own = CareRecipient(full_name=ERASED_NAME, created_by_user_id=booker.user_id)
    db.add(own)
    db.commit()
    r = client.post(f"{ADMIN}/care-plans", json={**plan_body(), "care_recipient_id": str(own.id)}, headers=rh)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "client_not_found"
    assert db.query(CarePlan).count() == 0


def test_a_second_active_plan_for_a_client_is_refused_with_the_existing_id(db, client, rh, plan):
    again = client.post(f"{ADMIN}/care-plans", json={**plan_body(), "patient_id": plan["subject"]["id"]}, headers=rh)
    assert again.status_code == 409
    assert again.json()["detail"] == {"code": "care_plan_exists", "message": "This client already has an active care plan.",
                                      "plan_id": plan["id"]}


def test_a_template_that_does_not_exist_is_template_not_found(db, client, rh):
    r = admin_plan(client, rh, a_patient(db), template_id="00000000-0000-4000-8000-000000000003")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "template_not_found"


def test_the_nurse_limits_apply_to_an_admin_too(db, client, rh):
    too_many = [{"title": f"Placeholder goal {i}", "frequency": "daily"} for i in range(31)]
    assert admin_plan(client, rh, a_patient(db), goals=too_many).status_code == 422


def test_the_services_own_refusals_are_invalid_care_plan_422s(db, client, rh, plan):
    no_risk = client.post(f"{ADMIN}/care-plans", headers=rh, json={"patient_id": str(a_patient(db).id), "diagnosis": "x"})
    assert no_risk.status_code == 422 and no_risk.json()["detail"] == {
        "code": "invalid_care_plan", "message": "risk_level is required"}
    backwards = client.post(f"{ADMIN}/care-plans/{plan['id']}/medications", headers=rh,
                            json=med_body(start_date="2026-10-28", end_date="2026-10-01"))
    assert backwards.status_code == 422 and backwards.json()["detail"]["code"] == "invalid_care_plan"
    for i in range(40):
        assert client.post(f"{ADMIN}/care-plans/{plan['id']}/medications", headers=rh,
                           json=med_body(name=f"Placeholder medicine {i}", end_date=None)).status_code == 201
    forty_first = client.post(f"{ADMIN}/care-plans/{plan['id']}/medications", headers=rh, json=med_body(end_date=None))
    assert forty_first.status_code == 422 and "at most 40" in forty_first.json()["detail"]["message"]


# --- the caregiver and the admin see one plan -----------------------------------------------------------------------

def test_a_nurse_sees_an_admin_authored_plan_and_the_admins_edits(db, client, rh):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    plan = admin_plan(client, rh, patient).json()
    seen = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()
    assert seen["author"] == {"name": "Placeholder Reviewer", "kind": "admin"} and seen["diagnosis"] == SECRET_DIAGNOSIS
    edited = client.patch(f"{ADMIN}/care-plans/{plan['id']}", headers=rh,
                          json={"expected_version": 1, "risk_level": "low"})
    assert edited.status_code == 200
    again = client.get(f"{BASE}/care-plans/{plan['id']}", headers=h(nurse)).json()
    assert again["risk_level"] == "low" and again["version"] == 2


def test_a_nurse_cannot_create_when_an_admin_already_made_the_plan(db, client, rh):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    plan = admin_plan(client, rh, patient).json()
    r = create_plan(client, nurse, booking)
    assert r.status_code == 409 and r.json()["detail"]["plan_id"] == plan["id"]


# --- list and open --------------------------------------------------------------------------------------------------

def test_the_list_has_no_plan_text_and_filters_by_status_and_name(db, client, rh):
    a = admin_plan(client, rh, a_patient(db, full_name="Placeholder Alpha")).json()
    b = admin_plan(client, rh, a_patient(db, full_name="Placeholder Bravo")).json()
    client.patch(f"{ADMIN}/care-plans/{b['id']}", headers=rh, json={"expected_version": 1, "status": "completed"})

    everything = client.get(f"{ADMIN}/care-plans", headers=rh)
    assert everything.status_code == 200
    body = everything.json()
    assert body["total"] == 2 and body["limit"] == 50 and body["offset"] == 0
    assert [i["id"] for i in body["items"]] == [b["id"], a["id"]]  # newest change first
    first = [i for i in body["items"] if i["id"] == a["id"]][0]
    assert first["goal_count"] == 1 and first["task_count"] == 1 and first["medication_count"] == 0
    assert first["client"]["full_name"] == "Placeholder Alpha" and first["subject"]["kind"] == "patient"
    assert first["author"]["kind"] == "admin"
    for secret in (SECRET_DIAGNOSIS, SECRET_GOAL, SECRET_TASK):
        assert secret not in everything.text
    assert "diagnosis" not in first

    active = client.get(f"{ADMIN}/care-plans?status=active", headers=rh).json()
    assert [i["id"] for i in active["items"]] == [a["id"]]
    by_name = client.get(f"{ADMIN}/care-plans?q=bravo", headers=rh).json()
    assert [i["id"] for i in by_name["items"]] == [b["id"]] and by_name["total"] == 1
    assert client.get(f"{ADMIN}/care-plans?q=%25", headers=rh).json()["total"] == 0  # a wildcard is just text
    page = client.get(f"{ADMIN}/care-plans?limit=1&offset=1", headers=rh).json()
    assert [i["id"] for i in page["items"]] == [a["id"]] and page["total"] == 2
    assert client.get(f"{ADMIN}/care-plans?status=bogus", headers=rh).status_code == 422
    assert client.get(f"{ADMIN}/care-plans?limit=201", headers=rh).status_code == 422


def test_the_list_counts_only_live_children(db, client, rh, plan):
    gid = plan["goals"][0]["id"]
    assert client.delete(f"{ADMIN}/care-plan-goals/{gid}?expected_version=1", headers=rh).status_code == 200
    item = client.get(f"{ADMIN}/care-plans", headers=rh).json()["items"][0]
    assert item["goal_count"] == 0 and item["task_count"] == 1


def test_opening_a_plan_returns_it_at_any_status(db, client, rh, plan):
    client.patch(f"{ADMIN}/care-plans/{plan['id']}", headers=rh, json={"expected_version": 1, "status": "archived"})
    r = client.get(f"{ADMIN}/care-plans/{plan['id']}", headers=rh)
    assert r.status_code == 200 and r.json()["status"] == "archived" and r.json()["diagnosis"] == SECRET_DIAGNOSIS
    assert r.headers["cache-control"] == "no-store"


def test_an_unknown_plan_is_one_404_everywhere(db, client, rh):
    missing = "00000000-0000-4000-8000-000000000009"
    expect = {"detail": "Care plan not found"}
    for method, path, body in (
        ("GET", f"/care-plans/{missing}", None), ("PATCH", f"/care-plans/{missing}", {"expected_version": 1}),
        ("GET", f"/care-plans/{missing}/observations", None), ("POST", f"/care-plans/{missing}/observations", {"body": "x"}),
        ("POST", f"/care-plans/{missing}/goals", {"title": "x", "frequency": "daily"}),
        ("PATCH", f"/care-plan-goals/{missing}", {"expected_version": 1}),
        ("DELETE", f"/care-plan-goals/{missing}?expected_version=1", None),
        ("POST", f"/care-plans/{missing}/tasks", {"title": "x", "time_slot": "morning", "days_of_week": [1]}),
        ("PATCH", f"/care-plan-tasks/{missing}", {"expected_version": 1}),
        ("DELETE", f"/care-plan-tasks/{missing}?expected_version=1", None),
        ("POST", f"/care-plans/{missing}/medications", med_body()),
        ("PATCH", f"/care-plan-medications/{missing}", {"expected_version": 1}),
        ("POST", f"/care-plan-medications/{missing}/stop", {"expected_version": 1}),
    ):
        r = client.request(method, ADMIN + path, json=body, headers=rh)
        assert (r.status_code, r.json()) == (404, expect), (method, path, r.text)


# --- edits: the same service as the nurse's ---------------------------------------------------------------------------

def test_patching_the_header_bumps_the_version_and_a_stale_one_gets_the_current_plan(db, client, rh, plan):
    ok = client.patch(f"{ADMIN}/care-plans/{plan['id']}", headers=rh,
                      json={"expected_version": 1, "diagnosis": "Placeholder diagnosis two", "allergies": ["Placeholder allergen two"]})
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.json()["diagnosis"] == "Placeholder diagnosis two"
    stale = client.patch(f"{ADMIN}/care-plans/{plan['id']}", headers=rh, json={"expected_version": 1, "risk_level": "low"})
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "version_conflict" and detail["plan"]["version"] == 2
    assert detail["plan"]["diagnosis"] == "Placeholder diagnosis two" and detail["plan"]["subject"]["kind"] == "patient"


def test_goals_tasks_and_medications_are_added_edited_and_removed(db, client, rh, plan):
    pid = plan["id"]
    goal = client.post(f"{ADMIN}/care-plans/{pid}/goals", json={"title": "Placeholder goal two", "frequency": "weekly"}, headers=rh)
    assert goal.status_code == 201 and goal.json()["plan_version"] == 2
    gid = goal.json()["goal"]["id"]
    edited = client.patch(f"{ADMIN}/care-plan-goals/{gid}", json={"expected_version": 2, "status": "achieved"}, headers=rh)
    assert edited.json()["goal"]["status"] == "achieved" and edited.json()["plan_version"] == 3

    task = client.post(f"{ADMIN}/care-plans/{pid}/tasks", headers=rh, json=daily_task("Placeholder task two", "evening", "18:00"))
    assert task.status_code == 201 and task.json()["plan_version"] == 4
    tid = task.json()["task"]["id"]
    moved = client.patch(f"{ADMIN}/care-plan-tasks/{tid}", json={"expected_version": 4, "time_slot": "night"}, headers=rh)
    assert moved.json()["task"]["time_slot"] == "night"
    gone = client.delete(f"{ADMIN}/care-plan-tasks/{tid}?expected_version=5", headers=rh)
    assert gone.status_code == 200 and gone.json()["plan_version"] == 6
    assert client.patch(f"{ADMIN}/care-plan-tasks/{tid}", json={"expected_version": 6, "title": "x"}, headers=rh).status_code == 404

    med = client.post(f"{ADMIN}/care-plans/{pid}/medications", json=med_body(), headers=rh)
    assert med.status_code == 201 and med.json()["medication"]["name"] == SECRET_MED
    mid = med.json()["medication"]["id"]
    v = med.json()["plan_version"]
    renamed = client.patch(f"{ADMIN}/care-plan-medications/{mid}", json={"expected_version": v, "dose_amount": "2"}, headers=rh)
    assert renamed.json()["medication"]["dose_amount"] == "2"
    stopped = client.post(f"{ADMIN}/care-plan-medications/{mid}/stop", json={"expected_version": v + 1}, headers=rh)
    assert stopped.json()["medication"]["status"] == "stopped" and stopped.json()["medication"]["stopped_at"]
    again = client.patch(f"{ADMIN}/care-plan-medications/{mid}", json={"expected_version": v + 2, "dose_amount": "3"}, headers=rh)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "medication_stopped"

    full = client.get(f"{ADMIN}/care-plans/{pid}", headers=rh).json()
    assert [m["status"] for m in full["medications"]] == ["stopped"] and len(full["tasks"]) == 1
    assert {g["status"] for g in full["goals"]} == {"active", "achieved"}


def test_a_completed_plan_refuses_every_write_but_stays_readable(db, client, rh, plan):
    pid = plan["id"]
    assert client.patch(f"{ADMIN}/care-plans/{pid}", headers=rh, json={"expected_version": 1, "status": "completed"}).status_code == 200
    for method, path, body in (
        ("PATCH", f"/care-plans/{pid}", {"expected_version": 2, "risk_level": "low"}),
        ("POST", f"/care-plans/{pid}/goals", {"title": "x", "frequency": "daily"}),
        ("POST", f"/care-plans/{pid}/tasks", daily_task()),
        ("POST", f"/care-plans/{pid}/medications", med_body()),
        ("POST", f"/care-plans/{pid}/observations", {"body": SECRET_NOTE}),
        ("DELETE", f"/care-plan-goals/{plan['goals'][0]['id']}?expected_version=2", None),
    ):
        r = client.request(method, ADMIN + path, json=body, headers=rh)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "plan_not_editable", (method, path, r.text)
    assert client.get(f"{ADMIN}/care-plans/{pid}", headers=rh).status_code == 200


def test_after_completing_the_client_can_get_a_new_active_plan(db, client, rh, plan):
    client.patch(f"{ADMIN}/care-plans/{plan['id']}", headers=rh, json={"expected_version": 1, "status": "completed"})
    again = client.post(f"{ADMIN}/care-plans", json={**plan_body(), "patient_id": plan["subject"]["id"]}, headers=rh)
    assert again.status_code == 201 and again.json()["id"] != plan["id"]


# --- notes ----------------------------------------------------------------------------------------------------------

def test_an_admin_note_is_attributed_listed_and_does_not_bump_the_version(db, client, rh, plan):
    r = client.post(f"{ADMIN}/care-plans/{plan['id']}/observations", json={"body": SECRET_NOTE, "category": "special_note"}, headers=rh)
    assert r.status_code == 201, r.text
    assert r.json()["author"] == {"name": "Placeholder Reviewer", "kind": "admin"} and r.json()["category"] == "special_note"
    listed = client.get(f"{ADMIN}/care-plans/{plan['id']}/observations", headers=rh).json()
    assert [n["body"] for n in listed["items"]] == [SECRET_NOTE] and listed["next_before"] is None
    assert client.get(f"{ADMIN}/care-plans/{plan['id']}", headers=rh).json()["version"] == 1
    only = client.get(f"{ADMIN}/care-plans/{plan['id']}/observations?category=observation", headers=rh).json()
    assert only["items"] == []


def test_a_nurses_note_shows_in_the_admin_list_with_its_author(db, client, rh):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    plan = admin_plan(client, rh, patient).json()
    client.post(f"{BASE}/care-plans/{plan['id']}/observations", json={"body": SECRET_NOTE}, headers=h(nurse))
    items = client.get(f"{ADMIN}/care-plans/{plan['id']}/observations", headers=rh).json()["items"]
    assert [(n["body"], n["author"]["kind"]) for n in items] == [(SECRET_NOTE, "caregiver")]


# --- templates ------------------------------------------------------------------------------------------------------

def test_the_template_list_shows_content_and_review_state(db, client, rh):
    r = client.get(f"{ADMIN}/care-plan-templates", headers=rh)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == db.query(CarePlanTemplate).count() == 5
    stroke = [t for t in items if t["key"] == "stroke_recovery"][0]
    assert stroke["clinical_review_status"] == "pending" and stroke["reviewed_by"] is None and stroke["reviewed_at"] is None
    assert stroke["is_active"] is True and stroke["goals"] and stroke["tasks"] and stroke["special_instructions"]


def test_a_review_records_who_and_when_and_can_be_taken_back(db, client, rh, reviewer):
    template = a_template(db)
    ok = client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", json={"status": "approved"}, headers=rh)
    assert ok.status_code == 200, ok.text
    assert ok.json()["clinical_review_status"] == "approved" and ok.json()["reviewed_by"] == "Placeholder Reviewer"
    assert ok.json()["reviewed_at"]
    db.expire_all()
    row = db.get(CarePlanTemplate, template.id)
    assert row.reviewed_by_admin_id == reviewer.id and row.clinical_review_status == "approved"
    listed = [t for t in client.get(f"{ADMIN}/care-plan-templates", headers=rh).json()["items"] if t["key"] == template.key][0]
    assert listed["clinical_review_status"] == "approved" and listed["reviewed_by"] == "Placeholder Reviewer"

    back = client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", json={"status": "pending"}, headers=rh)
    assert back.json()["clinical_review_status"] == "pending"
    assert client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", json={"status": "maybe"}, headers=rh).status_code == 422
    missing = client.post(f"{ADMIN}/care-plan-templates/00000000-0000-4000-8000-000000000004/review",
                          json={"status": "approved"}, headers=rh)
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "template_not_found"


def test_nurses_now_see_the_approved_status(db, client, rh):
    nurse = make_nurse(db)
    template = a_template(db)
    client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", json={"status": "approved"}, headers=rh)
    items = client.get(f"{BASE}/care-plan-templates", headers=h(nurse)).json()["items"]
    assert {t["key"]: t["clinical_review_status"] for t in items}[template.key] == "approved"


def test_events_name_the_admin_as_the_actor(db, client, rh, reviewer, plan):
    client.post(f"{ADMIN}/care-plans/{plan['id']}/goals", json={"title": "Placeholder goal two", "frequency": "daily"}, headers=rh)
    events = db.query(CarePlanEvent).filter(CarePlanEvent.plan_id == plan["id"]).order_by(CarePlanEvent.created_at).all()
    assert [(e.actor_type, e.actor_id, e.action) for e in events] == [
        ("admin", reviewer.id, "plan.create"), ("admin", reviewer.id, "goal.add")]
