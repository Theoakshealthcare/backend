"""0020_user_token_version: ``users.token_version`` is NOT NULL, existing users start at 0, and the downgrade drops it.

Only touches its own revision and the previous one, on a scratch database. Models and migrations agreeing is covered for
the whole history by test_migrations.py.
"""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0020_user_token_version", "0019_care_plan_admin_permissions"


def _columns(eng):
    return {c["name"]: c for c in inspect(eng).get_columns("users")}


def test_0020_follows_its_predecessor():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(THIS).down_revision == PREVIOUS


def test_existing_users_start_at_version_zero_and_the_downgrade_drops_the_column(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    assert "token_version" not in _columns(scratch)
    with scratch.begin() as conn:
        conn.execute(text("INSERT INTO users (id, full_name, hashed_password, user_type) "
                          "VALUES (gen_random_uuid(), 'Placeholder User', 'x', 'caregiver')"))

    _run(scratch, command.upgrade, THIS)
    column = _columns(scratch)["token_version"]
    assert column["nullable"] is False
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT token_version FROM users")).scalar_one() == 0

    _run(scratch, command.downgrade, PREVIOUS)
    assert "token_version" not in _columns(scratch)
