"""0018_care_plan_medications: the table, its constraints and the round trip.

Constraint tests run against the suite database (built by ``alembic upgrade head``); the round trip runs on a scratch one.
"""

from datetime import date, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db.models.care_plan import CarePlan, CarePlanMedication
from app.tests.care_plan_helpers import make_nurse, self_visit
from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0018_care_plan_medications", "0017_care_plan_ticks_and_notes"


def _plan(db):
    nurse = make_nurse(db)
    patient, _ = self_visit(db, nurse)
    plan = CarePlan(patient_id=patient.id, risk_level="low", diagnosis_encrypted="x", start_date=date.today(), duration_days=30,
                    created_by_caregiver_id=nurse.id)
    db.add(plan)
    db.commit()
    return plan


def _med(plan, **over):
    fields = dict(plan_id=plan.id, detail_encrypted="x", frequency="once_daily", start_date=date(2026, 10, 28))
    fields.update(over)
    return CarePlanMedication(**fields)


def _refused(db, row):
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_0018_follows_its_predecessor():
    # "Single head" is asserted for the whole history by test_migrations.py; 0018 is no longer the head once 0019 exists.
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_downgrade_round_trip(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    assert "care_plan_medications" in set(inspect(scratch).get_table_names())
    _run(scratch, command.downgrade, PREVIOUS)
    names = set(inspect(scratch).get_table_names())
    assert "care_plan_medications" not in names and "care_plan_observations" in names
    _run(scratch, command.upgrade, THIS)
    assert "care_plan_medications" in set(inspect(scratch).get_table_names())


def test_defaults_are_active_and_no_times(db):
    plan = _plan(db)
    row = _med(plan)
    db.add(row)
    db.commit()
    db.refresh(row)
    assert row.status == "active" and row.stopped_at is None and row.end_date is None and row.times_of_day == []


def test_the_frequency_and_status_are_closed_sets(db):
    plan = _plan(db)
    _refused(db, _med(plan, frequency="hourly"))
    _refused(db, _med(plan, status="paused"))


def test_stopped_and_stopped_at_go_together(db):
    plan = _plan(db)
    _refused(db, _med(plan, status="stopped"))
    _refused(db, _med(plan, status="active", stopped_at=datetime.utcnow()))
    db.add(_med(plan, status="stopped", stopped_at=datetime.utcnow()))
    db.commit()


def test_an_end_date_cannot_precede_the_start_date(db):
    plan = _plan(db)
    _refused(db, _med(plan, end_date=date(2026, 10, 27)))
    db.add(_med(plan, end_date=date(2026, 10, 28)))
    db.commit()


def test_at_most_twelve_times_a_day_and_the_plan_and_frequency_and_start_are_required(db):
    plan = _plan(db)
    _refused(db, _med(plan, times_of_day=[f"{h:02d}:00" for h in range(13)]))
    for missing in ("plan_id", "frequency", "start_date"):
        _refused(db, _med(plan, **{missing: None}))
