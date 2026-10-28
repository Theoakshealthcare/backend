"""Care plans, admin API: every admin write and every read of plan text leaves an ``admin_audit_log`` row in the same
transaction, and no row ever carries plan text (Finance can read the audit log, ``audit.read``)."""

import json

import pytest
from sqlalchemy import text

from app.db.models.audit import AdminAuditLog
from app.db.models.care_plan import CarePlan, CarePlanEvent
from app.tests.care_plan_helpers import (
    SECRET_ALLERGY, SECRET_DIAGNOSIS, SECRET_GOAL, SECRET_INSTRUCTION, SECRET_MED_TEXTS, SECRET_NOTE, SECRET_TARGET,
    SECRET_TASK, SECRET_TEXTS, a_template, daily_task, med_body,
)
from app.tests.factories import auth_headers, make_admin
from app.tests.test_admin_care_plans import ADMIN, a_patient, admin_plan, plan, rh, reviewer  # noqa: F401  (fixtures)

EVERYTHING_SECRET = SECRET_TEXTS + SECRET_MED_TEXTS + (SECRET_NOTE, "Placeholder diagnosis two", "Placeholder goal two")


def audit_rows(db, plan_id=None):
    query = db.query(AdminAuditLog).order_by(AdminAuditLog.created_at, AdminAuditLog.id)
    return [r for r in query if plan_id is None or r.entity_id == str(plan_id)]


def test_a_full_authoring_session_audits_each_write_and_no_row_holds_plan_text(db, client, rh, reviewer, plan):
    pid = plan["id"]
    v = 1
    client.patch(f"{ADMIN}/care-plans/{pid}", headers=rh, json={"expected_version": v, "diagnosis": "Placeholder diagnosis two",
                                                                "allergies": [SECRET_ALLERGY]})
    goal = client.post(f"{ADMIN}/care-plans/{pid}/goals", json={"title": "Placeholder goal two", "frequency": "daily"}, headers=rh).json()
    task = client.post(f"{ADMIN}/care-plans/{pid}/tasks", json=daily_task(), headers=rh).json()
    med = client.post(f"{ADMIN}/care-plans/{pid}/medications", json=med_body(), headers=rh).json()
    client.patch(f"{ADMIN}/care-plan-goals/{goal['goal']['id']}", headers=rh, json={"expected_version": 5, "title": "x"})
    client.delete(f"{ADMIN}/care-plan-tasks/{task['task']['id']}?expected_version=6", headers=rh)
    client.patch(f"{ADMIN}/care-plan-medications/{med['medication']['id']}", headers=rh,
                 json={"expected_version": 7, "note": SECRET_NOTE})
    client.post(f"{ADMIN}/care-plan-medications/{med['medication']['id']}/stop", json={"expected_version": 8}, headers=rh)
    client.post(f"{ADMIN}/care-plans/{pid}/observations", json={"body": SECRET_NOTE}, headers=rh)
    client.get(f"{ADMIN}/care-plans/{pid}", headers=rh)
    client.get(f"{ADMIN}/care-plans/{pid}/observations", headers=rh)

    rows = audit_rows(db, pid)
    assert [r.action for r in rows] == [
        "care_plan.plan.create", "care_plan.plan.update", "care_plan.goal.add", "care_plan.task.add", "care_plan.med.add",
        "care_plan.goal.update", "care_plan.task.remove", "care_plan.med.update", "care_plan.med.stop",
        "care_plan.observation.add", "care_plan.view", "care_plan.observations_view"]
    assert all(r.admin_user_id == reviewer.id and r.entity_type == "care_plan" for r in rows)
    assert rows[1].after == {"fields": ["diagnosis", "allergies"]}
    assert rows[10].after == {"fields": ["diagnosis", "allergies", "special_instructions", "goals", "medications", "tasks"]}

    dump = json.dumps([[r.action, r.entity_id, r.before, r.after, r.reason] for r in db.query(AdminAuditLog)])
    for secret in EVERYTHING_SECRET:
        assert secret not in dump, secret
    events = json.dumps([e.detail for e in db.query(CarePlanEvent)])
    for secret in EVERYTHING_SECRET:
        assert secret not in events, secret


def test_the_audit_row_records_the_request_origin(db, client, rh, plan):
    row = audit_rows(db, plan["id"])[0]
    assert row.user_agent == "testclient" or row.user_agent is not None
    assert row.ip is not None


def test_a_refused_write_leaves_no_write_row_and_no_event(db, client, rh, plan):
    over = client.post(f"{ADMIN}/care-plans", headers=rh, json={**{"diagnosis": "x", "risk_level": "low"},
                                                                "patient_id": plan["subject"]["id"]})
    assert over.status_code == 409  # carries only the existing plan's id, no plan text
    missing = client.post(f"{ADMIN}/care-plans", headers=rh, json={"diagnosis": "x", "risk_level": "low",
                                                                    "patient_id": "00000000-0000-4000-8000-000000000001"})
    assert missing.status_code == 404
    done = client.patch(f"{ADMIN}/care-plans/{plan['id']}", headers=rh, json={"expected_version": 1, "status": "completed"})
    assert done.status_code == 200
    before = (len(audit_rows(db)), db.query(CarePlanEvent).count())
    refused = client.post(f"{ADMIN}/care-plans/{plan['id']}/goals", headers=rh, json={"title": "x", "frequency": "daily"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "plan_not_editable"
    assert (len(audit_rows(db)), db.query(CarePlanEvent).count()) == before
    db.expire_all()
    assert db.get(CarePlan, plan["id"]).version == 2


def test_a_stale_version_409_shows_the_plan_so_it_is_audited_as_a_read_and_changes_nothing(db, client, rh, plan):
    events = db.query(CarePlanEvent).count()
    before = len(audit_rows(db))
    for method, path, body in (
        ("PATCH", f"/care-plans/{plan['id']}", {"expected_version": 9, "risk_level": "low"}),
        ("PATCH", f"/care-plan-goals/{plan['goals'][0]['id']}", {"expected_version": 9, "status": "achieved"}),
        ("DELETE", f"/care-plan-tasks/{plan['tasks'][0]['id']}?expected_version=9", None),
    ):
        r = client.request(method, ADMIN + path, json=body, headers=rh)
        assert r.status_code == 409 and r.json()["detail"]["plan"]["diagnosis"] == SECRET_DIAGNOSIS, path
    rows = audit_rows(db)[before:]
    assert [r.action for r in rows] == ["care_plan.view"] * 3
    assert all(r.after["via"] == "version_conflict" for r in rows)
    assert db.query(CarePlanEvent).count() == events
    db.expire_all()
    assert db.get(CarePlan, plan["id"]).version == 1


def test_an_edit_that_changes_nothing_returns_plan_text_so_it_is_audited_as_a_read(db, client, rh, plan):
    events = db.query(CarePlanEvent).count()
    before = len(audit_rows(db))
    gid, tid = plan["goals"][0]["id"], plan["tasks"][0]["id"]
    assert client.patch(f"{ADMIN}/care-plans/{plan['id']}", headers=rh, json={"expected_version": 1}).status_code == 200
    assert client.patch(f"{ADMIN}/care-plan-goals/{gid}", headers=rh, json={"expected_version": 1}).json()["plan_version"] == 1
    removed = client.delete(f"{ADMIN}/care-plan-tasks/{tid}?expected_version=1", headers=rh)
    assert removed.status_code == 200
    again = client.delete(f"{ADMIN}/care-plan-tasks/{tid}?expected_version=2", headers=rh)  # already removed: a no-op
    assert again.status_code == 200 and again.json()["task"]["title"] == SECRET_TASK
    rows = audit_rows(db)[before:]
    assert [r.action for r in rows] == ["care_plan.view", "care_plan.view", "care_plan.task.remove", "care_plan.view"]
    assert [r.after.get("via") for r in rows] == ["unchanged_write", "unchanged_write", None, "unchanged_write"]
    assert db.query(CarePlanEvent).count() == events + 1  # only the real removal is a plan event


def test_a_bare_admin_user_cannot_be_the_actor(db, reviewer, plan):
    from app.services import care_plans

    with pytest.raises(TypeError):
        care_plans.actor_event(db, plan["id"], reviewer, "goal.add", {})


def test_the_audit_and_the_change_commit_together(db, client, rh, plan, monkeypatch):
    """If the commit fails, neither the change nor its audit row survives."""
    from app.services import care_plans

    original = care_plans.AuditService.record

    def boom(self, **kw):
        original(self, **kw)
        raise RuntimeError("after the audit row was added")

    monkeypatch.setattr(care_plans.AuditService, "record", boom)
    before = len(audit_rows(db))
    r = client.post(f"{ADMIN}/care-plans/{plan['id']}/goals", json={"title": "Placeholder goal two", "frequency": "daily"}, headers=rh)
    assert r.status_code == 500
    db.expire_all()
    assert len(audit_rows(db)) == before
    assert db.execute(text("SELECT count(*) FROM care_plan_goals WHERE plan_id = :p"), {"p": plan["id"]}).scalar_one() == 1
    assert db.get(CarePlan, plan["id"]).version == 1


def test_template_review_is_audited_without_the_template_content(db, client, rh, reviewer):
    template = a_template(db)
    client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", json={"status": "approved"}, headers=rh)
    client.post(f"{ADMIN}/care-plan-templates/{template.id}/review", json={"status": "approved"}, headers=rh)
    rows = [r for r in audit_rows(db) if r.action == "care_plan_template.review"]
    assert len(rows) == 2 and rows[0].entity_type == "care_plan_template" and rows[0].entity_id == str(template.id)
    assert rows[0].before == {"clinical_review_status": "pending"}
    assert rows[0].after == {"clinical_review_status": "approved", "key": template.key, "version": template.version}
    assert rows[1].before == {"clinical_review_status": "approved"}  # a second sign-off is still recorded


def test_listing_plans_and_templates_is_not_audited(db, client, rh, plan):
    before = len(audit_rows(db))
    client.get(f"{ADMIN}/care-plans", headers=rh)
    client.get(f"{ADMIN}/care-plan-templates", headers=rh)
    assert len(audit_rows(db)) == before


def test_finance_can_read_the_audit_trail_of_plan_writes_and_sees_no_plan_text(db, client, rh, plan):
    finance = auth_headers(make_admin(db, ["finance"], password=None))
    r = client.get(f"{ADMIN}/audit?entity_type=care_plan", headers=finance)
    assert r.status_code == 200
    assert r.json()["total"] >= 1
    for secret in EVERYTHING_SECRET:
        assert secret not in r.text


def test_a_validation_error_never_echoes_the_rejected_text(db, client, rh, plan):
    r = client.post(f"{ADMIN}/care-plans/{plan['id']}/goals", headers=rh,
                    json={"title": SECRET_GOAL, "frequency": "not-a-frequency", "target": SECRET_TARGET})
    assert r.status_code == 422
    assert SECRET_GOAL not in r.text and SECRET_TARGET not in r.text and "input" not in str(r.json())


def test_every_admin_care_plan_response_is_no_store(db, client, rh, plan):
    pid = plan["id"]
    for method, path, body in (
        ("GET", "/care-plans", None), ("GET", f"/care-plans/{pid}", None), ("GET", f"/care-plans/{pid}/observations", None),
        ("GET", "/care-plan-templates", None), ("PATCH", f"/care-plans/{pid}", {"expected_version": 9}),
        ("GET", f"/care-plans/00000000-0000-4000-8000-000000000009", None),
    ):
        r = client.request(method, ADMIN + path, json=body, headers=rh)
        assert r.headers.get("cache-control") == "no-store", (path, r.status_code)
