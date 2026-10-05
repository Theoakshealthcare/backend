"""The Alembic history builds the whole schema from an empty database and matches the models."""

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.session import engine as suite_engine


def _cfg(connection=None) -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["configure_logger"] = False
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture
def empty_engine():
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


def test_single_head():
    assert len(ScriptDirectory.from_config(_cfg()).get_heads()) == 1


def test_upgrade_from_empty_matches_models_and_downgrades_cleanly(empty_engine):
    with empty_engine.begin() as conn:
        command.upgrade(_cfg(conn), "head")
    with empty_engine.connect() as conn:
        diffs = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diffs == []
    assert set(Base.metadata.tables) <= set(inspect(empty_engine).get_table_names())

    with empty_engine.begin() as conn:
        command.downgrade(_cfg(conn), "base")
    assert inspect(empty_engine).get_table_names() == ["alembic_version"]

    # and back up again (enum types were dropped on downgrade)
    with empty_engine.begin() as conn:
        command.upgrade(_cfg(conn), "head")


def test_admin_audit_log_is_append_only_in_the_database(db):
    from app.tests.factories import make_admin

    admin = make_admin(db)
    db.execute(
        text("INSERT INTO admin_audit_log (id, admin_user_id, action, entity_type, created_at) "
             "VALUES (:id, :a, 'test', 'x', now())"),
        {"id": uuid.uuid4(), "a": admin.id},
    )
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE admin_audit_log SET action = 'tampered'"))
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM admin_audit_log"))
    db.rollback()
