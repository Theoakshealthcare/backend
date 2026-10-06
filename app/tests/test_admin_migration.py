"""0003_admin_identity: new tables, seeded roles, re-pointed foreign keys, and a downgrade that has data.

Only touches its own revision on a scratch database (never "head"). Models and migrations agreeing
across the whole history is covered by test_migrations.py.
"""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from app.core.admin_permissions import ROLE_PERMISSIONS
from app.db.session import engine as suite_engine

THIS, PREVIOUS = "0003_admin_identity", "0002_caregiver_mobile"
NEW_TABLES = {"admin_users", "admin_roles", "admin_role_permissions", "admin_user_roles",
              "admin_refresh_tokens", "admin_rate_limits"}


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


def _tables(eng):
    return set(inspect(eng).get_table_names())


def _fk_targets(eng, table):
    return {fk["referred_table"] for fk in inspect(eng).get_foreign_keys(table)}


def test_up_down_up(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    assert "admins" in _tables(scratch) and not NEW_TABLES & _tables(scratch)

    _run(scratch, command.upgrade, THIS)
    assert NEW_TABLES <= _tables(scratch) and "admins" not in _tables(scratch)
    for table in ("caregivers", "caregiver_documents", "caregiver_skills", "admin_audit_log"):
        assert "admin_users" in _fk_targets(scratch, table), table
    assert "users" not in _fk_targets(scratch, "admin_audit_log")

    _run(scratch, command.downgrade, PREVIOUS)
    assert "admins" in _tables(scratch) and not NEW_TABLES & _tables(scratch)
    assert "users" in _fk_targets(scratch, "admin_audit_log")

    _run(scratch, command.upgrade, THIS)
    assert NEW_TABLES <= _tables(scratch)


def test_roles_and_permissions_are_seeded(scratch):
    _run(scratch, command.upgrade, THIS)
    with scratch.connect() as conn:
        rows = conn.execute(text(
            "SELECT r.key, p.permission FROM admin_roles r JOIN admin_role_permissions p ON p.role_id = r.id")).all()
    seeded = {}
    for key, perm in rows:
        seeded.setdefault(key, set()).add(perm)
    assert seeded == {k: set(v) for k, v in ROLE_PERMISSIONS.items()}


def test_downgrade_keeps_audit_rows_and_clears_admin_references(scratch):
    _run(scratch, command.upgrade, THIS)
    admin_id, audit_id = uuid.uuid4(), uuid.uuid4()
    with scratch.begin() as conn:
        conn.execute(text(
            "INSERT INTO admin_users (id, email, full_name, status, mfa_enabled, failed_logins, created_at, updated_at)"
            " VALUES (:i, 'a@example.com', 'A', 'active', false, 0, now(), now())"), {"i": admin_id})
        conn.execute(text(
            "INSERT INTO admin_audit_log (id, admin_user_id, action, entity_type, created_at)"
            " VALUES (:i, :a, 'x', 'y', now())"), {"i": audit_id, "a": admin_id})

    _run(scratch, command.downgrade, PREVIOUS)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM admin_audit_log")).scalar() == 1
    # the audit log is still append-only after the round trip
    with scratch.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text("UPDATE admin_audit_log SET action = 'tampered'"))
