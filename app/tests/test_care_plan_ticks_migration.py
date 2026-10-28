"""0017_care_plan_ticks_and_notes: the two tables, their constraints and the round trip.

Constraint tests run against the suite database (built by ``alembic upgrade head``); the round trip runs on a scratch one.
"""

from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db.models.care_plan import CarePlan, CarePlanObservation, CarePlanTask, CarePlanTaskCompletion
from app.tests.care_plan_helpers import make_nurse, self_visit
from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0017_care_plan_ticks_and_notes", "0016_care_plans_core"
TABLES = {"care_plan_task_completions", "care_plan_observations"}


def _scene(db):
    nurse = make_nurse(db)
    patient, booking = self_visit(db, nurse)
    plan = CarePlan(patient_id=patient.id, risk_level="low", diagnosis_encrypted="x", start_date=date.today(), duration_days=30,
                    created_by_caregiver_id=nurse.id)
    db.add(plan)
    db.flush()
    task = CarePlanTask(plan_id=plan.id, title_encrypted="x", time_slot="morning", days_of_week=[1])
    db.add(task)
    db.commit()
    return nurse, booking, plan, task


def _refused(db, *rows):
    db.add_all(rows)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_0017_follows_its_predecessor():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_downgrade_round_trip(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    assert TABLES <= set(inspect(scratch).get_table_names())
    _run(scratch, command.downgrade, PREVIOUS)
    names = set(inspect(scratch).get_table_names())
    assert not TABLES & names and "care_plan_tasks" in names  # only this migration's tables go
    _run(scratch, command.upgrade, THIS)
    assert TABLES <= set(inspect(scratch).get_table_names())


def test_one_tick_per_task_per_day(db):
    nurse, booking, _, task = _scene(db)
    tick = dict(task_id=task.id, completed_by_caregiver_id=nurse.id, booking_id=booking.id)
    db.add(CarePlanTaskCompletion(for_date=date(2026, 10, 28), **tick))
    db.commit()
    _refused(db, CarePlanTaskCompletion(for_date=date(2026, 10, 28), **tick))
    db.add(CarePlanTaskCompletion(for_date=date(2026, 10, 29), **tick))  # another day is fine
    db.commit()


def test_a_tick_needs_its_task_caregiver_and_booking(db):
    nurse, booking, _, task = _scene(db)
    good = dict(task_id=task.id, for_date=date(2026, 10, 28), completed_by_caregiver_id=nurse.id, booking_id=booking.id)
    for missing in ("task_id", "completed_by_caregiver_id", "booking_id", "for_date"):
        _refused(db, CarePlanTaskCompletion(**{**good, missing: None}))


def test_a_note_has_exactly_one_author_and_a_known_category(db):
    nurse, booking, plan, _ = _scene(db)
    _refused(db, CarePlanObservation(plan_id=plan.id, category="observation", body_encrypted="x"))  # no author
    _refused(db, CarePlanObservation(plan_id=plan.id, author_caregiver_id=nurse.id, category="diary", body_encrypted="x"))
    ok = CarePlanObservation(plan_id=plan.id, author_caregiver_id=nurse.id, body_encrypted="x")
    db.add(ok)
    db.commit()
    assert ok.category == "observation" and ok.family_visible is False and ok.booking_id is None
