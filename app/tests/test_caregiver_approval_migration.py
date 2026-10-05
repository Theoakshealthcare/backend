"""E5 migration: reversible, keeps existing rows, and agrees with the models.

Runs in a scratch database on the same Postgres as the test suite. The schema starts from the
models (create_all), is taken back to the previous revision with the migration's own
downgrade(), seeded rows survive, then upgrade() must reproduce exactly what the models define.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.session import engine as suite_engine

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
NEW_TABLES = {"caregiver_skills", "admin_audit_log"}


def _load_migration():
    (path,) = VERSIONS.glob("*_caregiver_approval.py")
    spec = importlib.util.spec_from_file_location("caregiver_approval_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


@pytest.fixture
def scratch_engine():
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


def test_migration_round_trip_with_seeded_rows(scratch_engine):
    mig = _load_migration()
    assert mig.down_revision == "6fb30639d5f4"
    Base.metadata.create_all(scratch_engine)

    user_id, cg_id, doc_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with scratch_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, phone_number, full_name, hashed_password, user_type, "
            "is_active, is_verified) VALUES (:id, 'a@example.com', '+2348000000001', 'A', 'x', "
            "'caregiver', true, true)"), {"id": user_id})
        conn.execute(text(
            "INSERT INTO caregivers (id, user_id, email, full_name, profile_status, "
            "background_check_status) VALUES (:id, :u, 'a@example.com', 'A', 'approved', 'in_progress')"),
            {"id": cg_id, "u": user_id})
        conn.execute(text(
            "INSERT INTO caregiver_documents (id, caregiver_id, document_type, document_name, file_path) "
            "VALUES (:id, :c, 'license', 'l.pdf', 'k')"), {"id": doc_id, "c": cg_id})

    # down to the previous revision
    _run(scratch_engine, mig.downgrade)
    insp = inspect(scratch_engine)
    assert not NEW_TABLES & set(insp.get_table_names())
    assert "approved_by" not in {c["name"] for c in insp.get_columns("caregivers")}
    assert "reviewed_by" not in {c["name"] for c in insp.get_columns("caregiver_documents")}
    with scratch_engine.connect() as conn:  # seeded rows survived the downgrade
        assert conn.execute(text("SELECT profile_status FROM caregivers")).scalar() == "approved"

    # and back up
    _run(scratch_engine, mig.upgrade)
    insp = inspect(scratch_engine)
    assert NEW_TABLES <= set(insp.get_table_names())
    with scratch_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT profile_status, background_check_status, approved_by, changes_requested "
            "FROM caregivers")).one()
        assert tuple(row) == ("approved", "in_progress", None, None)
        assert conn.execute(text("SELECT count(*) FROM caregiver_documents")).scalar() == 1

    # the upgraded schema equals what the models define
    with scratch_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []

    # the audit table is append-only for any client
    with scratch_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO admin_audit_log (id, admin_user_id, action, entity_type, created_at) "
            "VALUES (:id, :u, 'x.y', 'x', now())"), {"id": uuid.uuid4(), "u": user_id})
    for statement in ("UPDATE admin_audit_log SET reason = 'tamper'", "DELETE FROM admin_audit_log"):
        with pytest.raises(DBAPIError, match="append-only"):
            with scratch_engine.begin() as conn:
                conn.execute(text(statement))

    # second full cycle: down and up again cleanly (audit rows are dropped with the table)
    _run(scratch_engine, mig.downgrade)
    _run(scratch_engine, mig.upgrade)
    with scratch_engine.connect() as conn:
        assert diff == compare_metadata(MigrationContext.configure(conn), Base.metadata)
