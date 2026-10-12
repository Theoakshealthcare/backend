"""0012_caregiver_payouts: the new tables and constraints, a clean round trip, and what the downgrade keeps.

Only touches its own revision and the previous one, on a scratch database (see test_dropout_migration for the pattern).
"""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0012_caregiver_payouts", "0011_caregiver_earnings"
NEW_TABLES = {"payout_batches", "payouts", "caregiver_bank_events", "payout_attempts"}
BANK_COLUMNS = {"bank_code", "recipient_code", "verified_at", "payout_blocked_until"}


def _tables(eng):
    return set(inspect(eng).get_table_names())


def _cols(eng, table):
    return {c["name"] for c in inspect(eng).get_columns(table)}


def _seed_caregiver(conn):
    uid, cid = uuid.uuid4(), uuid.uuid4()
    conn.execute(text("INSERT INTO users (id, email, full_name, user_type, hashed_password, is_active) VALUES "
                      "(:u, :e, 'T', 'caregiver', 'x', true)"), {"u": uid, "e": f"{uid.hex}@example.com"})
    conn.execute(text("INSERT INTO caregivers (id, user_id, email, full_name, phone_number) VALUES "
                      "(:c, :u, :e, 'T', '+2348000000000')"), {"c": cid, "u": uid, "e": f"{uid.hex}@example.com"})
    return cid


def test_0012_follows_0011():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_adds_everything_and_downgrade_removes_it_and_up_again_works(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    assert not NEW_TABLES & _tables(scratch) and not BANK_COLUMNS & _cols(scratch, "caregiver_bank_accounts")
    _run(scratch, command.upgrade, THIS)
    assert NEW_TABLES <= _tables(scratch) and BANK_COLUMNS <= _cols(scratch, "caregiver_bank_accounts")
    _run(scratch, command.downgrade, PREVIOUS)
    assert not NEW_TABLES & _tables(scratch) and not BANK_COLUMNS & _cols(scratch, "caregiver_bank_accounts")
    _run(scratch, command.upgrade, THIS)
    assert NEW_TABLES <= _tables(scratch)


def test_the_database_refuses_bad_payout_data(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    with scratch.begin() as conn:
        cid = _seed_caregiver(conn)
        conn.execute(text("INSERT INTO payout_batches (id, period_key, cadence, min_payout_kobo, status, created_at, updated_at) "
                          "VALUES (:i, '2026-10-12', 'weekly', 500000, 'pending_approval', now(), now())"), {"i": uuid.uuid4()})
        batch = conn.execute(text("SELECT id FROM payout_batches")).scalar()
    def insert_tx(conn, kind="payout", status="pending_approval"):
        tx = uuid.uuid4()
        conn.execute(text("INSERT INTO caregiver_transactions (id, caregiver_id, type, amount, status) "
                          "VALUES (:i, :c, :t, 100, :s)"), {"i": tx, "c": cid, "t": kind, "s": status})
        return tx

    def insert_payout(conn, tx, *, status="processing", amount=100, reference=None):
        conn.execute(text("INSERT INTO payouts (id, batch_id, caregiver_id, transaction_id, amount_kobo, status, bank_name, "
                          "bank_code, account_number, account_name, recipient_code, reference, attempts, created_at, updated_at) "
                          "VALUES (:i, :b, :c, :t, :a, :s, 'B', '058', '0', 'N', 'R', :r, 1, now(), now())"),
                     {"i": uuid.uuid4(), "b": batch, "c": cid, "t": tx, "a": amount, "s": status,
                      "r": reference or f"po-{uuid.uuid4().hex}-1"})

    with scratch.begin() as conn:
        insert_payout(conn, insert_tx(conn))  # one live payout is fine
    for build in (
        lambda c: insert_payout(c, insert_tx(c)),                                  # a second live one for the same caregiver
        lambda c: insert_payout(c, insert_tx(c), status="paused"),                 # unknown status
        lambda c: insert_payout(c, insert_tx(c), status="failed", amount=0),       # a zero payout
        lambda c: insert_tx(c, kind="refund"),                                     # unknown ledger type
        lambda c: insert_tx(c, status="exploded"),                                 # unknown ledger status
        lambda c: c.execute(text("UPDATE caregiver_wallets SET balance = -1")) if c.execute(
            text("INSERT INTO caregiver_wallets (id, caregiver_id, balance) VALUES (:i, :c, 5)"),
            {"i": uuid.uuid4(), "c": cid}) else None,                              # a negative balance
        lambda c: c.execute(text("INSERT INTO payout_batches (id, period_key, cadence, min_payout_kobo, status, created_at, "
                                 "updated_at) VALUES (:i, '2026-10-12', 'weekly', 1, 'pending_approval', now(), now())"),
                            {"i": uuid.uuid4()}),                                  # the same payout day twice
    ):
        with pytest.raises(DBAPIError):
            with scratch.begin() as conn:
                build(conn)
    with scratch.begin() as conn:  # a failed one next to a live one is allowed (the live-payout index is partial)
        insert_payout(conn, insert_tx(conn, status="failed"), status="failed")


def test_one_live_payout_per_caregiver_needs_attention_is_not_live_and_attempt_references_are_unique(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    with scratch.begin() as conn:
        cid = _seed_caregiver(conn)
        conn.execute(text("INSERT INTO payout_batches (id, period_key, cadence, min_payout_kobo, status, created_at, updated_at) "
                          "VALUES (:i, '2026-10-12', 'weekly', 500000, 'pending_approval', now(), now())"), {"i": uuid.uuid4()})
        batch = conn.execute(text("SELECT id FROM payout_batches")).scalar()

    def insert_payout(conn, status, shortfall=0):
        tx, payout = uuid.uuid4(), uuid.uuid4()
        conn.execute(text("INSERT INTO caregiver_transactions (id, caregiver_id, type, amount, status) "
                          "VALUES (:i, :c, 'payout', 100, 'pending')"), {"i": tx, "c": cid})
        conn.execute(text("INSERT INTO payouts (id, batch_id, caregiver_id, transaction_id, amount_kobo, status, bank_name, "
                          "bank_code, account_number, account_name, recipient_code, reference, attempts, shortfall_kobo, "
                          "created_at, updated_at) VALUES (:i, :b, :c, :t, 100, :s, 'B', '058', '0', 'N', 'R', :r, 1, :f, "
                          "now(), now())"), {"i": payout, "b": batch, "c": cid, "t": tx, "s": status, "f": shortfall,
                                             "r": f"po-{uuid.uuid4().hex}-1"})
        return payout

    def insert_attempt(conn, payout, reference):
        conn.execute(text("INSERT INTO payout_attempts (id, payout_id, attempt, reference, status, created_at) "
                          "VALUES (:i, :p, 1, :r, 'pending', now())"), {"i": uuid.uuid4(), "p": payout, "r": reference})

    with scratch.begin() as conn:
        first = insert_payout(conn, "needs_attention")
        insert_attempt(conn, first, "po-attempt-reference-0001")
        insert_payout(conn, "needs_attention")  # needs_attention is not in the live-payout index: the freeze blocks batches
        insert_payout(conn, "pending_approval")  # so a live payout may sit beside it
    for build in (
        lambda c: insert_payout(c, "processing"),                                  # a second live payout
        lambda c: insert_payout(c, "failed", shortfall=-1),                        # a negative shortfall
        lambda c: insert_attempt(c, first, "po-attempt-reference-0002"),           # attempt 1 twice for one payout
        lambda c: insert_attempt(c, insert_payout(c, "failed"), "po-attempt-reference-0001"),  # a reused reference
    ):
        with pytest.raises(DBAPIError):
            with scratch.begin() as conn:
                build(conn)


def test_the_wallet_freeze_columns_exist_and_default_to_not_frozen(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    with scratch.begin() as conn:
        cid = _seed_caregiver(conn)
        conn.execute(text("INSERT INTO caregiver_wallets (id, caregiver_id, balance) VALUES (:i, :c, 100)"),
                     {"i": uuid.uuid4(), "c": cid})
        row = conn.execute(text("SELECT payouts_frozen_at, payouts_frozen_reason FROM caregiver_wallets")).one()
    assert tuple(row) == (None, None)
    _run(scratch, command.downgrade, PREVIOUS)
    with scratch.connect() as conn:
        columns = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns "
                                                   "WHERE table_name = 'caregiver_wallets'"))}
    assert not {"payouts_frozen_at", "payouts_frozen_reason"} & columns


def test_downgrade_removes_payout_ledger_rows_and_keeps_everything_else(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    with scratch.begin() as conn:
        cid = _seed_caregiver(conn)
        for kind in ("payout", "withdrawal", "earning"):
            conn.execute(text("INSERT INTO caregiver_transactions (id, caregiver_id, type, amount, status) VALUES "
                              "(:i, :c, :t, 100, 'pending')"), {"i": uuid.uuid4(), "c": cid, "t": kind})
    _run(scratch, command.downgrade, PREVIOUS)
    with scratch.connect() as conn:
        kinds = sorted(r[0] for r in conn.execute(text("SELECT type FROM caregiver_transactions")))
    assert kinds == ["earning", "withdrawal"]


def test_an_account_saved_before_the_migration_survives_unverified(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    with scratch.begin() as conn:
        cid = _seed_caregiver(conn)
        conn.execute(text("INSERT INTO caregiver_bank_accounts (id, caregiver_id, bank_name, account_number, account_name) "
                          "VALUES (:i, :c, 'Typed Bank', '0123456789', 'Typed')"), {"i": uuid.uuid4(), "c": cid})
    _run(scratch, command.upgrade, THIS)
    with scratch.connect() as conn:
        row = conn.execute(text("SELECT bank_name, bank_code, recipient_code, verified_at FROM caregiver_bank_accounts")).one()
    assert tuple(row) == ("Typed Bank", None, None, None)  # kept, and not paid until it is saved again through the verified flow
