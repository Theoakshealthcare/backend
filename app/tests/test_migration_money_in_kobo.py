"""Migration 68b8941cfc28 (money in kobo) on seeded rows, in a scratch database.

The scratch database is built from the current models (kobo columns) and stamped at the
migration; `downgrade -1` then produces the pre-migration column types, old-format rows with
fractional naira are seeded, and the upgrade is checked value by value. The same procedure
against a database built by origin/main's `init_local_db` is recorded in the SOP.
Alembic runs in a subprocess because its env.py reconfigures logging and reads DATABASE_URL
at import time.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.migration

REVISION = "68b8941cfc28"
ROOT = Path(__file__).resolve().parents[2]


def test_single_alembic_head_and_chain():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == "6fb30639d5f4"


@pytest.fixture
def scratch_url():
    base = make_url(os.environ["DATABASE_URL"])
    name = f"kobo_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic(url, *args):
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def _insert(conn, table, **values):
    """Insert one row: given values, plus a dummy for every other NOT NULL column."""
    cols = conn.execute(text(
        "SELECT column_name, data_type, udt_name FROM information_schema.columns "
        "WHERE table_name = :t AND is_nullable = 'NO' AND column_default IS NULL"
    ), {"t": table}).all()
    row = dict(values)
    for name, data_type, udt in cols:
        if name in row:
            continue
        if data_type == "uuid":
            row[name] = str(uuid.uuid4())
        elif data_type in ("character varying", "text"):
            row[name] = "x"
        elif data_type.startswith("timestamp"):
            row[name] = "2026-01-01 00:00:00"
        elif data_type == "boolean":
            row[name] = False
        elif data_type == "date":
            row[name] = "2026-01-01"
        elif data_type in ("json", "jsonb"):
            row[name] = "{}"
        elif data_type == "USER-DEFINED":
            row[name] = conn.execute(text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :u ORDER BY e.enumsortorder LIMIT 1"
            ), {"u": udt}).scalar_one()
        else:  # integer / float / numeric
            row[name] = 0
    keys = ", ".join(f'"{k}"' for k in row)
    params = ", ".join(f":{k}" for k in row)
    conn.execute(text(f'INSERT INTO "{table}" ({keys}) VALUES ({params})'), row)


# table, old-format values (naira), then the expected kobo after upgrade
SEED = [
    ("bookings", {"total_amount": 1531.885, "service_fee": 0.1 + 0.2, "platform_fee": 44000.0},
     {"total_amount_kobo": 153189, "service_fee_kobo": 30, "platform_fee_kobo": 4_400_000}),
    ("booking_settings", {"base_fee": 1234.567}, {"base_fee_kobo": 123457}),
    ("patient_wallets",
     {"balance": "1234.56", "total_deposited": "99999999.99", "total_spent": "0.01", "total_refunded": "0.00"},
     {"balance_kobo": 123456, "total_deposited_kobo": 9_999_999_999, "total_spent_kobo": 1,
      "total_refunded_kobo": 0}),
    ("patient_transactions", {"amount": "5000.50"}, {"amount_kobo": 500050}),
    ("patient_refund_requests", {"amount": "0.05"}, {"amount_kobo": 5}),
    ("prescriptions", {"total_cost": 2500.499}, {"total_cost_kobo": 250050}),
    ("prescription_settings", {"base_fee": 5000.0}, {"base_fee_kobo": 500000}),
]


def _read(engine, table, columns):
    with engine.connect() as conn:
        return dict(conn.execute(text(f'SELECT {", ".join(columns)} FROM "{table}"')).mappings().one())


def test_upgrade_downgrade_round_trip_on_seeded_rows(scratch_url):
    import app.db.models  # noqa: F401
    from app.db.base import Base

    engine = create_engine(scratch_url)
    try:
        Base.metadata.create_all(engine)
        _alembic(scratch_url, "stamp", REVISION)
        _alembic(scratch_url, "downgrade", "-1")  # now the pre-migration column types

        with engine.begin() as conn:
            conn.execute(text("SET session_replication_role = replica"))  # skip FK checks for seeding
            for table, old_values, _ in SEED:
                _insert(conn, table, **old_values)
            # a NULL cost must stay NULL
            _insert(conn, "prescriptions", total_cost=None)

        _alembic(scratch_url, "upgrade", "head")

        for table, _, expected in SEED:
            if table == "prescriptions":
                with engine.connect() as conn:
                    got = sorted(
                        conn.execute(text("SELECT total_cost_kobo FROM prescriptions")).scalars().all(),
                        key=lambda v: (v is None, v),
                    )
                assert got == [250050, None]
                continue
            got = _read(engine, table, list(expected))
            assert got == expected, table

        with engine.connect() as conn:
            types = dict(conn.execute(text(
                "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
                "WHERE column_name LIKE '%\\_kobo'"
            )).all())
        assert len(types) == 12 and set(types.values()) == {"bigint"}

        # models and migration agree: nothing left for autogenerate to change
        from alembic.autogenerate import compare_metadata
        from alembic.runtime.migration import MigrationContext

        with engine.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                     Base.metadata)
        money_diffs = [d for d in diffs if "kobo" in repr(d) or "total_amount" in repr(d)]
        assert money_diffs == []

        _alembic(scratch_url, "downgrade", "-1")
        with engine.connect() as conn:
            assert float(conn.execute(text("SELECT balance FROM patient_wallets")).scalar_one()) == pytest.approx(1234.56)
            assert float(conn.execute(text("SELECT total_deposited FROM patient_wallets")).scalar_one()) \
                == pytest.approx(99999999.99)
            assert conn.execute(text("SELECT total_amount FROM bookings")).scalar_one() == pytest.approx(1531.89)
            assert conn.execute(text("SELECT base_fee FROM booking_settings")).scalar_one() == pytest.approx(1234.57)
            assert float(conn.execute(text("SELECT amount FROM patient_transactions")).scalar_one()) == pytest.approx(5000.5)

        _alembic(scratch_url, "upgrade", "head")
        assert _read(engine, "patient_wallets", ["balance_kobo"]) == {"balance_kobo": 123456}
    finally:
        engine.dispose()
