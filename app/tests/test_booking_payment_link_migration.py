"""0023_booking_payment_link: the stored Paystack link columns, nullable, with a clean downgrade."""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0023_booking_payment_link", "0022_password_reset_code"
NEW_COLUMNS = {"paystack_authorization_url", "paystack_access_code"}


def _columns(eng) -> dict:
    return {c["name"]: c for c in inspect(eng).get_columns("bookings")}


def test_0023_follows_0022_and_the_history_has_one_head():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_revision(THIS).down_revision == PREVIOUS
    assert len(scripts.get_heads()) == 1


def test_upgrade_adds_nullable_columns_and_downgrade_removes_them(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    assert not NEW_COLUMNS & set(_columns(scratch))
    _run(scratch, command.upgrade, THIS)
    cols = _columns(scratch)
    assert NEW_COLUMNS <= set(cols) and all(cols[c]["nullable"] for c in NEW_COLUMNS)
    _run(scratch, command.downgrade, PREVIOUS)
    assert not NEW_COLUMNS & set(_columns(scratch))
    _run(scratch, command.upgrade, THIS)
    assert NEW_COLUMNS <= set(_columns(scratch))
