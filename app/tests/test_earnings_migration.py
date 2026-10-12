"""0011_caregiver_earnings: the policy columns (seeded), the booking snapshot, the transaction columns and constraints.

Only touches its own revision and the previous one, on a scratch database (see test_dropout_migration for the pattern).
"""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.db.session import engine as suite_engine
from app.tests.test_dropout_migration import _cfg, _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0011_caregiver_earnings", "0010_dropout_fee_tiers"
POLICY_COLUMNS = {"caregiver_visit_share_percent", "earning_hold_hours", "payout_cadence", "payout_min_naira",
                  "finance_refund_limit_naira"}
TX_COLUMNS = {"booking_id", "base_amount_kobo", "share_percent", "available_at", "released_at", "status_reason",
              "status_changed_by", "status_changed_at"}


def _cols(eng, table):
    return {c["name"] for c in inspect(eng).get_columns(table)}


def test_0011_follows_0010():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_seeds_the_defaults_and_downgrade_removes_everything(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    assert not POLICY_COLUMNS & _cols(scratch, "booking_policies")
    _run(scratch, command.upgrade, THIS)
    assert POLICY_COLUMNS <= _cols(scratch, "booking_policies") and TX_COLUMNS <= _cols(scratch, "caregiver_transactions")
    assert "earnings_terms" in _cols(scratch, "bookings")
    with scratch.connect() as conn:
        row = conn.execute(text("SELECT caregiver_visit_share_percent, earning_hold_hours, payout_cadence, payout_min_naira, "
                                "finance_refund_limit_naira, caregiver_late_fee_share_percent FROM booking_policies")).one()
    assert tuple(row) == (80, 24, "weekly", 5000, 50000, 80)

    _run(scratch, command.downgrade, PREVIOUS)
    assert not POLICY_COLUMNS & _cols(scratch, "booking_policies")
    assert not TX_COLUMNS & _cols(scratch, "caregiver_transactions") and "earnings_terms" not in _cols(scratch, "bookings")
    _run(scratch, command.upgrade, THIS)  # and up again
    assert POLICY_COLUMNS <= _cols(scratch, "booking_policies")


@pytest.mark.parametrize("assignment", [
    "caregiver_visit_share_percent = 101", "caregiver_visit_share_percent = -1", "earning_hold_hours = 169",
    "payout_cadence = 'daily'", "payout_min_naira = -1", "finance_refund_limit_naira = 100000001",
])
def test_the_database_refuses_invalid_earnings_settings(scratch, assignment):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    with sessionmaker(bind=scratch)() as s:
        with pytest.raises(DBAPIError):
            s.execute(text(f"UPDATE booking_policies SET {assignment}"))
            s.commit()


def test_existing_earning_rows_and_withdrawals_survive_the_upgrade_and_downgrade_keeps_withdrawals(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    uid, cid = uuid.uuid4(), uuid.uuid4()
    with scratch.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email, full_name, user_type, hashed_password, is_active) VALUES "
                          "(:u, :e, 'T', 'caregiver', 'x', true)"), {"u": uid, "e": f"{uid.hex}@example.com"})
        conn.execute(text("INSERT INTO caregivers (id, user_id, email, full_name, phone_number) VALUES "
                          "(:c, :u, :e, 'T', '+2348000000000')"), {"c": cid, "u": uid, "e": f"{uid.hex}@example.com"})
        conn.execute(text("INSERT INTO caregiver_transactions (id, caregiver_id, type, amount, status) VALUES "
                          "(:i, :c, 'withdrawal', 100, 'pending')"), {"i": uuid.uuid4(), "c": cid})
    _run(scratch, command.upgrade, THIS)
    _run(scratch, command.downgrade, PREVIOUS)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM caregiver_transactions WHERE type = 'withdrawal'")).scalar() == 1
