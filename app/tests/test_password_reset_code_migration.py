"""0022_password_reset_code: two nullable columns on ``users``, existing rows untouched, the downgrade drops them.

Only touches its own revision and the previous one, on a scratch database. Models and migrations agreeing is covered for
the whole history by test_migrations.py.
"""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0022_password_reset_code", "0021_patient_notifications"
COLUMNS = ("password_reset_code_hash", "password_reset_code_expires_at")


def _columns(eng):
    return {c["name"]: c for c in inspect(eng).get_columns("users")}


def test_0022_follows_its_predecessor():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(THIS).down_revision == PREVIOUS


def test_the_columns_are_nullable_existing_users_keep_working_and_the_downgrade_drops_them(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    assert not set(COLUMNS) & set(_columns(scratch))
    with scratch.begin() as conn:
        conn.execute(text("INSERT INTO users (id, full_name, hashed_password, user_type) "
                          "VALUES (gen_random_uuid(), 'Placeholder User', 'x', 'caregiver')"))

    _run(scratch, command.upgrade, THIS)
    columns = _columns(scratch)
    assert all(columns[name]["nullable"] for name in COLUMNS)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT password_reset_code_hash, password_reset_code_expires_at FROM users")).one() == (None, None)

    _run(scratch, command.downgrade, PREVIOUS)
    assert not set(COLUMNS) & set(_columns(scratch))
