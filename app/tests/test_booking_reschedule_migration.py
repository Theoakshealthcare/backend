"""0034_booking_reschedule: reschedule_count, the booking_reschedules table and the three policy settings."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0034_booking_reschedule", "0030_bank_name_review"


def _cols(eng, table):
    return {c["name"]: c for c in inspect(eng).get_columns(table)}


def test_0034_follows_0030_and_the_history_has_one_head():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_revision(THIS).down_revision == PREVIOUS and len(scripts.get_heads()) == 1


def test_upgrade_adds_everything_with_defaults_and_downgrade_removes_it(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    assert "reschedule_count" not in _cols(scratch, "bookings") and "booking_reschedules" not in inspect(scratch).get_table_names()
    _run(scratch, command.upgrade, THIS)
    assert _cols(scratch, "bookings")["reschedule_count"]["nullable"] is False
    assert {"reschedule_free_hours", "reschedule_max_free", "reschedule_max_days_ahead"} <= set(_cols(scratch, "booking_policies"))
    assert "booking_reschedules" in inspect(scratch).get_table_names()
    with scratch.begin() as conn:
        row = conn.execute(text("SELECT reschedule_free_hours, reschedule_max_free, reschedule_max_days_ahead "
                                "FROM booking_policies")).one()
        assert tuple(row) == (12, 2, 28)
    _run(scratch, command.downgrade, PREVIOUS)
    assert "reschedule_count" not in _cols(scratch, "bookings") and "booking_reschedules" not in inspect(scratch).get_table_names()
    assert "reschedule_max_free" not in _cols(scratch, "booking_policies")
    _run(scratch, command.upgrade, THIS)


@pytest.mark.parametrize("column, value", [("reschedule_max_days_ahead", 0), ("reschedule_max_days_ahead", 366),
                                           ("reschedule_max_free", 21), ("reschedule_free_hours", 169),
                                           ("reschedule_free_hours", -1)])
def test_the_database_refuses_settings_out_of_range(scratch, column, value):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    with sessionmaker(bind=scratch)() as s:
        with pytest.raises(DBAPIError):
            s.execute(text(f"UPDATE booking_policies SET {column} = {value}"))
            s.commit()
