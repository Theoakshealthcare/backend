"""0029_location_nudged_at: a nullable timestamp on caregiver_locations and a clean round trip (scratch database)."""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.tests.test_caregiver_applying_as_migration import _caregiver
from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

BEFORE, THIS = "0028_caregiver_applying_as", "0029_location_nudged_at"


def _columns(eng):
    return {c["name"] for c in inspect(eng).get_columns("caregiver_locations")}


def test_the_revision_chains_and_the_history_has_one_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision(THIS).down_revision == BEFORE
    assert len(script.get_heads()) == 1  # a later migration may sit on top; two heads would be a fork


def test_upgrade_adds_a_nullable_column_and_downgrade_removes_it(scratch):
    _run(scratch, command.upgrade, BEFORE)
    with scratch.begin() as conn:
        _caregiver(conn, 1)
        conn.execute(text("INSERT INTO caregiver_locations (id, caregiver_id, latitude, longitude) "
                          "SELECT gen_random_uuid(), id, 6.45, 3.47 FROM caregivers"))
    assert "location_nudged_at" not in _columns(scratch)

    _run(scratch, command.upgrade, THIS)
    assert "location_nudged_at" in _columns(scratch)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT location_nudged_at FROM caregiver_locations")).scalar() is None  # existing rows: never nudged

    _run(scratch, command.downgrade, BEFORE)
    assert "location_nudged_at" not in _columns(scratch)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM caregiver_locations")).scalar() == 1  # the row survives

    _run(scratch, command.upgrade, THIS)  # and it can go up again
    assert "location_nudged_at" in _columns(scratch)
