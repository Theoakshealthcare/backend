"""0010_dropout_fee_tiers: the policy table (seeded), the booking columns, the drop-out record, the new events.

Only touches its own revision and the previous one, on a scratch database, so later migrations don't break it.
Models and migrations agreeing is covered for the whole history by test_migrations.py.
"""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.db.session import engine as suite_engine

THIS, PREVIOUS = "0010_dropout_fee_tiers", "0009_account_erasure"
NEW_COLUMNS = {"cancel_policy", "cancel_fee_tier", "late_cancel_compensable", "on_my_way_at", "on_my_way_distance_m",
               "dropout_state"}
NEW_EVENTS = ("caregiver_dropped_replacing", "dropout_choice_needed", "dropout_handed_over")


def _cfg(connection) -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["configure_logger"] = False
    cfg.attributes["connection"] = connection
    return cfg


def _run(eng, fn, revision):
    with eng.begin() as conn:
        fn(_cfg(conn), revision)


@pytest.fixture
def scratch():
    name = f"mig_{uuid.uuid4().hex[:10]}"
    with suite_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    eng = create_engine(suite_engine.url.set(database=name))
    try:
        yield eng
    finally:
        eng.dispose()
        with suite_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _booking_columns(eng):
    return {c["name"] for c in inspect(eng).get_columns("bookings")}


def _event_check(eng) -> str:
    with eng.connect() as conn:
        return conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_booking_events_event'")).scalar()


def test_0010_is_in_the_history_and_follows_0009():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert THIS in {r.revision for r in scripts.walk_revisions()}
    assert scripts.get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_adds_everything_and_downgrade_removes_it_cleanly(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    assert "booking_policies" not in inspect(scratch).get_table_names()
    assert not NEW_COLUMNS & _booking_columns(scratch)
    assert not any(e in _event_check(scratch) for e in NEW_EVENTS)

    _run(scratch, command.upgrade, THIS)
    tables = inspect(scratch).get_table_names()
    assert {"booking_policies", "caregiver_dropouts"} <= set(tables)
    assert NEW_COLUMNS <= _booking_columns(scratch)
    assert all(e in _event_check(scratch) for e in NEW_EVENTS)
    with scratch.connect() as conn:
        row = conn.execute(text("SELECT * FROM booking_policies")).mappings().all()
    assert len(row) == 1 and dict(row[0]) | {"updated_at": None, "updated_by": None} == {
        "id": 1, "cancel_free_hours": 12, "cancel_late_hours": 2, "cancel_fee_percent": 20,
        "cancel_late_fee_percent": 50, "dropout_redispatch_min_lead_minutes": 120, "dropout_decision_lead_minutes": 60,
        "onmyway_compensation_window_minutes": 120, "caregiver_late_fee_share_percent": 80,
        "updated_at": None, "updated_by": None}

    _run(scratch, command.downgrade, PREVIOUS)
    tables = inspect(scratch).get_table_names()
    assert "booking_policies" not in tables and "caregiver_dropouts" not in tables
    assert not NEW_COLUMNS & _booking_columns(scratch)
    assert not any(e in _event_check(scratch) for e in NEW_EVENTS)

    _run(scratch, command.upgrade, THIS)  # and up again
    assert NEW_COLUMNS <= _booking_columns(scratch)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM booking_policies")).scalar() == 1


@pytest.mark.parametrize("assignment", [
    "cancel_fee_percent = 101", "cancel_late_fee_percent = -1", "caregiver_late_fee_share_percent = 101",
    "cancel_late_hours = 12", "cancel_free_hours = 1", "dropout_decision_lead_minutes = 120",
    "onmyway_compensation_window_minutes = -5", "cancel_free_hours = 169", "dropout_redispatch_min_lead_minutes = 10081",
    "dropout_decision_lead_minutes = 10081, dropout_redispatch_min_lead_minutes = 10080",
    "onmyway_compensation_window_minutes = 10081", "cancel_fee_percent = 60",
])
def test_the_database_itself_refuses_an_invalid_policy(scratch, assignment):
    _run(scratch, command.upgrade, THIS)
    with sessionmaker(bind=scratch)() as s:
        with pytest.raises(DBAPIError):
            s.execute(text(f"UPDATE booking_policies SET {assignment}"))
            s.commit()


def test_the_policy_is_a_single_row(scratch):
    _run(scratch, command.upgrade, THIS)
    with sessionmaker(bind=scratch)() as s:
        with pytest.raises(DBAPIError):
            s.execute(text(
                "INSERT INTO booking_policies SELECT 2, cancel_free_hours, cancel_late_hours, cancel_fee_percent, "
                "cancel_late_fee_percent, dropout_redispatch_min_lead_minutes, dropout_decision_lead_minutes, "
                "onmyway_compensation_window_minutes, caregiver_late_fee_share_percent, updated_at, updated_by "
                "FROM booking_policies"))
            s.commit()
