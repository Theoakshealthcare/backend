"""0016_care_plans_core: the five tables, their constraints and the seeded templates.

Constraint tests run against the suite database (built by ``alembic upgrade head``); the round trip runs on a scratch one.
"""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db.models.care_plan import CarePlan, CarePlanEvent, CarePlanGoal, CarePlanTask, CarePlanTemplate
from app.tests.care_plan_helpers import make_nurse, self_visit
from app.tests.factories import make_caregiver, make_patient
from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0016_care_plans_core", "0015_caregiver_kyc"
TABLES = {"care_plan_templates", "care_plans", "care_plan_goals", "care_plan_tasks", "care_plan_events"}


def _plan(patient=None, recipient_id=None, **over):
    fields = dict(patient_id=patient.id if patient else None, care_recipient_id=recipient_id, risk_level="low",
                  diagnosis_encrypted="x", start_date="2026-10-28", duration_days=30)
    fields.update(over)
    return CarePlan(**fields)


def _refused(db, *rows):
    db.add_all(rows)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_0016_follows_its_predecessor():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_downgrade_round_trip(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    assert TABLES <= set(inspect(scratch).get_table_names())
    _run(scratch, command.downgrade, PREVIOUS)
    assert not TABLES & set(inspect(scratch).get_table_names())
    _run(scratch, command.upgrade, THIS)
    assert TABLES <= set(inspect(scratch).get_table_names())


def test_five_templates_are_seeded_pending_review_and_active(db):
    rows = db.query(CarePlanTemplate).all()
    assert len(rows) == 5
    assert all(t.clinical_review_status == "pending" and t.is_active and t.version == 1 for t in rows)
    assert all(t.reviewed_by_admin_id is None and t.reviewed_at is None for t in rows)
    for t in rows:
        body = t.body
        assert set(body) == {"goals", "tasks", "special_instructions"}
        assert body["goals"] and body["tasks"] and body["special_instructions"]
        assert all(set(task) == {"title", "time_slot", "time_of_day", "days_of_week"} for task in body["tasks"])


def test_a_plan_has_exactly_one_subject_and_exactly_one_author(db):
    patient, nurse = make_patient(db), make_nurse(db)
    _refused(db, _plan(created_by_caregiver_id=nurse.id))  # no subject
    _refused(db, _plan(patient, recipient_id=patient.id, created_by_caregiver_id=nurse.id))  # two subjects (second FK fails too)
    _refused(db, _plan(patient))  # no author


def test_one_active_plan_per_client_but_history_is_allowed(db):
    patient, nurse = make_patient(db), make_nurse(db)
    done = _plan(patient, created_by_caregiver_id=nurse.id, status="completed")
    db.add_all([done, _plan(patient, created_by_caregiver_id=nurse.id, status="archived"),
                _plan(patient, created_by_caregiver_id=nurse.id)])
    db.commit()  # two finished plans and one active: fine
    _refused(db, _plan(patient, created_by_caregiver_id=nurse.id))  # a second active one is not
    other = make_patient(db)
    db.add(_plan(other, created_by_caregiver_id=nurse.id))  # another client's active plan is
    db.commit()


@pytest.mark.parametrize("over", [{"status": "paused"}, {"risk_level": "extreme"}, {"duration_days": 0}, {"duration_days": 366},
                                  {"review_every_days": 6}, {"review_every_days": 91}])
def test_plan_check_constraints(db, over):
    patient, nurse = make_patient(db), make_nurse(db)
    _refused(db, _plan(patient, created_by_caregiver_id=nurse.id, **over))


def _a_plan(db):
    patient, nurse = make_patient(db), make_nurse(db)
    plan = _plan(patient, created_by_caregiver_id=nurse.id)
    db.add(plan)
    db.commit()
    return plan


def test_child_check_constraints_and_defaults(db):
    plan = _a_plan(db)
    _refused(db, CarePlanGoal(plan_id=plan.id, frequency="hourly"))
    _refused(db, CarePlanGoal(plan_id=plan.id, frequency="daily", status="paused"))
    _refused(db, CarePlanTask(plan_id=plan.id, time_slot="noon", days_of_week=[1]))
    _refused(db, CarePlanTask(plan_id=plan.id, time_slot="morning", days_of_week=[]))
    _refused(db, CarePlanEvent(plan_id=plan.id, actor_type="robot", action="x", detail={}))
    goal, task = CarePlanGoal(plan_id=plan.id, frequency="daily"), CarePlanTask(plan_id=plan.id, time_slot="morning", days_of_week=[1])
    db.add_all([goal, task])
    db.commit()
    assert goal.family_visible is False and goal.status == "active"
    assert task.family_visible is False and task.is_active is True
    assert plan.version == 1 and plan.status == "active"


def test_template_check_constraints(db):
    _refused(db, CarePlanTemplate(key="x", name="x", summary="x", duration_days=1, default_risk_level="extreme", body={}))
    _refused(db, CarePlanTemplate(key="y", name="y", summary="y", duration_days=1, default_risk_level="low", body={},
                                  clinical_review_status="maybe"))
    _refused(db, CarePlanTemplate(key="stroke_recovery", name="dup", summary="d", duration_days=1,
                                  default_risk_level="low", body={}))  # keys are unique
