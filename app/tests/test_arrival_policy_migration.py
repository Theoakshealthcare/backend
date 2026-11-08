"""0024_arrival_policy_cancel_note: arrived_at, cancel_note, and the short-visit threshold on the policy row."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0024_arrival_policy_cancel_note", "0023_booking_payment_link"


def _cols(eng, table):
    return {c["name"]: c for c in inspect(eng).get_columns(table)}


def test_0024_follows_0023_and_the_history_has_one_head():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_revision(THIS).down_revision == PREVIOUS and len(scripts.get_heads()) == 1


def test_upgrade_adds_the_columns_keeps_the_threshold_at_50_and_downgrade_removes_them(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    assert "short_visit_min_percent" not in _cols(scratch, "booking_policies")
    _run(scratch, command.upgrade, THIS)
    assert {"arrived_at", "cancel_note"} <= set(_cols(scratch, "bookings"))
    assert _cols(scratch, "bookings")["arrived_at"]["nullable"] and _cols(scratch, "bookings")["cancel_note"]["nullable"]
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT short_visit_min_percent FROM booking_policies")).scalar() == 50
    _run(scratch, command.downgrade, PREVIOUS)
    assert not {"arrived_at", "cancel_note"} & set(_cols(scratch, "bookings"))
    assert "short_visit_min_percent" not in _cols(scratch, "booking_policies")
    _run(scratch, command.upgrade, THIS)


@pytest.mark.parametrize("value", [-1, 101])
def test_the_database_refuses_a_threshold_outside_0_to_100(scratch, value):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    with sessionmaker(bind=scratch)() as s:
        with pytest.raises(DBAPIError):
            s.execute(text(f"UPDATE booking_policies SET short_visit_min_percent = {value}"))
