"""E3+E4 migration: reversible, keeps existing rows, and agrees with the models.

Only this revision's own upgrade()/downgrade() are exercised (never "head"), so the test stays
valid when later migrations are chained on top. The schema starts from the models (create_all),
is taken back with downgrade(), seeded rows survive, then upgrade() must reproduce exactly what
the models define.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.session import engine as suite_engine

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "app" / "alembic" / "versions"
NEW_BOOKING_COLUMNS = {"payment_expires_at", "cancel_reason", "dispatch_state", "dispatch_started_at"}


def _load_migration():
    (path,) = VERSIONS.glob("*_jobs_and_dispatch.py")
    spec = importlib.util.spec_from_file_location("jobs_and_dispatch_migration", path)
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


def test_alembic_history_has_exactly_one_head():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1


def test_migration_is_in_the_chain():
    mig = _load_migration()
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_revision(mig.revision) is not None
    assert mig.down_revision is not None


def test_migration_round_trip_with_seeded_rows(scratch_engine):
    mig = _load_migration()
    Base.metadata.create_all(scratch_engine)

    user_id, patient_id, cg_id, booking_id = (uuid.uuid4() for _ in range(4))
    with scratch_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, phone_number, full_name, hashed_password, user_type, "
            "is_active, is_verified) VALUES (:id, 'p@example.com', '+2348000000002', 'P', 'x', "
            "'patient', true, true)"), {"id": user_id})
        conn.execute(text(
            "INSERT INTO patients (id, user_id, email, full_name) VALUES (:id, :u, 'p@example.com', 'P')"),
            {"id": patient_id, "u": user_id})
        conn.execute(text(
            "INSERT INTO bookings (id, booking_code, patient_id, service_type, assistance_type, "
            "duration_hours, scheduled_date, scheduled_end_date, patient_address, patient_latitude, "
            "patient_longitude, patient_city, patient_state, status, payment_status, total_amount_kobo, "
            "service_fee_kobo, platform_fee_kobo, booking_for, dispatch_state) "
            "VALUES (:id, 'MIGTEST1', :p, 'nurse', 'x', 2, now(), now(), 'a', 6.4, 3.4, 'Lagos', 'Lagos', "
            "'PENDING', 'PAID', 100, 90, 10, 'myself', 'dispatching')"), {"id": booking_id, "p": patient_id})
        conn.execute(text(
            "INSERT INTO booking_dispatch_waves (id, booking_id, wave_no, notified_caregiver_ids, sent_at, "
            "expires_at) VALUES (:id, :b, 1, ARRAY[CAST(:c AS uuid)], now(), now())"),
            {"id": uuid.uuid4(), "b": booking_id, "c": cg_id})

    # down to the previous revision
    _run(scratch_engine, mig.downgrade)
    insp = inspect(scratch_engine)
    assert "booking_dispatch_waves" not in insp.get_table_names()
    assert not NEW_BOOKING_COLUMNS & {c["name"] for c in insp.get_columns("bookings")}
    with scratch_engine.connect() as conn:  # the seeded booking survived the downgrade
        assert tuple(conn.execute(text("SELECT booking_code, status FROM bookings")).one()) == (
            "MIGTEST1", "PENDING")

    # and back up
    _run(scratch_engine, mig.upgrade)
    insp = inspect(scratch_engine)
    assert "booking_dispatch_waves" in insp.get_table_names()
    assert NEW_BOOKING_COLUMNS <= {c["name"] for c in insp.get_columns("bookings")}
    with scratch_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT booking_code, dispatch_state, payment_expires_at, cancel_reason FROM bookings")).one()
        assert tuple(row) == ("MIGTEST1", None, None, None)  # legacy rows keep NULLs
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []

    # constraints the dispatcher relies on
    unique_cols = [tuple(u["column_names"]) for u in insp.get_unique_constraints("booking_dispatch_waves")]
    assert ("booking_id", "wave_no") in unique_cols

    # second full cycle
    _run(scratch_engine, mig.downgrade)
    _run(scratch_engine, mig.upgrade)
    with scratch_engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
