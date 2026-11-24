"""0028_caregiver_applying_as: the nullable column, its allow-list check and a clean downgrade (scratch database)."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

BEFORE, THIS = "0027_booking_minutes", "0028_caregiver_applying_as"


def _columns(eng):
    return {c["name"] for c in inspect(eng).get_columns("caregivers")}


def _caregiver(conn, n, applying_as=None):
    conn.execute(text("INSERT INTO users (id, full_name, hashed_password, user_type) "
                      f"VALUES ('00000000-0000-0000-0000-00000000010{n}', 'Placeholder User', 'x', 'caregiver')"))
    cols, vals = "", ""
    if applying_as is not None:
        cols, vals = ", applying_as", f", ARRAY{applying_as!r}::varchar[]"
    conn.execute(text("INSERT INTO caregivers (id, user_id, email, full_name" + cols + ") VALUES "
                      f"(gen_random_uuid(), '00000000-0000-0000-0000-00000000010{n}', 'p{n}@example.com', 'P'" + vals + ")"))


def test_the_revision_chains():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(THIS).down_revision == BEFORE


def test_upgrade_adds_a_nullable_checked_column_and_downgrade_removes_it(scratch):
    _run(scratch, command.upgrade, BEFORE)
    with scratch.begin() as conn:
        _caregiver(conn, 1)  # a row that predates the field
    assert "applying_as" not in _columns(scratch)

    _run(scratch, command.upgrade, THIS)
    assert "applying_as" in _columns(scratch)
    with scratch.begin() as conn:
        assert conn.execute(text("SELECT applying_as FROM caregivers")).scalar() is None  # legacy rows stay NULL
        _caregiver(conn, 2, ["nurse", "caregiver"])
    with pytest.raises(IntegrityError):
        with scratch.begin() as conn:
            _caregiver(conn, 3, ["surgeon"])

    _run(scratch, command.downgrade, BEFORE)
    assert "applying_as" not in _columns(scratch)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_caregivers_applying_as'")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM caregivers")).scalar() == 2  # the rows survive

    _run(scratch, command.upgrade, THIS)  # and it can go up again
    assert "applying_as" in _columns(scratch)
