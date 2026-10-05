"""0002_caregiver_mobile: push_tokens, the en_route status and the visit-code columns.

Only touches its own revision (never "head") on a scratch database, so later migrations don't
break it. Models and migrations agreeing is covered for the whole history by test_migrations.py.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.core.security import encrypt
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.db.session import engine as suite_engine

THIS, PREVIOUS = "0002_caregiver_mobile", "0001_baseline"


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


def _booking_columns(eng):
    return {c["name"] for c in inspect(eng).get_columns("bookings")}


def _enum_labels(eng):
    with eng.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'bookingstatus' ORDER BY e.enumsortorder"))]


def _seed(eng, status):
    Session = sessionmaker(bind=eng)
    with Session() as s:
        tag = uuid.uuid4().hex[:8]
        pu = User(email=f"p{tag}@example.com", full_name="P", hashed_password="x", user_type="patient")
        cu = User(email=f"c{tag}@example.com", full_name="C", hashed_password="x", user_type="caregiver")
        s.add_all([pu, cu])
        s.flush()
        patient = Patient(user_id=pu.id, email=pu.email, full_name="P", city="Lagos", state="Lagos")
        cg = Caregiver(user_id=cu.id, email=cu.email, full_name="C")
        s.add_all([patient, cg])
        s.flush()
        start = datetime.utcnow() + timedelta(days=1)
        booking = Booking(
            booking_code=tag.upper(), patient_id=patient.id, caregiver_id=cg.id, service_type="nurse",
            assistance_type="x", duration_hours=1, scheduled_date=start, scheduled_end_date=start,
            patient_address="a", patient_latitude=1.0, patient_longitude=1.0, patient_city="Lagos",
            patient_state="Lagos", total_amount_kobo=100, service_fee_kobo=90, platform_fee_kobo=10,
            booking_for="myself", status=status, payment_status=PaymentStatus.PAID,
            visit_code_enc=encrypt("4821"),
        )
        s.add(booking)
        s.add(PushToken(user_id=pu.id, token=f"ExponentPushToken[placeholder-{tag}]", platform="ios"))
        s.commit()
        return booking.id


def test_upgrade_adds_everything_and_downgrade_removes_it_cleanly(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    assert "push_tokens" not in inspect(scratch).get_table_names()
    assert "visit_code_enc" not in _booking_columns(scratch)
    assert "EN_ROUTE" not in _enum_labels(scratch)

    _run(scratch, command.upgrade, THIS)
    insp = inspect(scratch)
    assert "push_tokens" in insp.get_table_names()
    assert {"visit_code_enc", "visit_code_failed_attempts", "visit_code_locked_until"} <= _booking_columns(scratch)
    assert _enum_labels(scratch) == ["PENDING", "ACCEPTED", "EN_ROUTE", "IN_PROGRESS", "COMPLETED",
                                     "CANCELLED", "DECLINED"]
    assert any(i["unique"] and i["column_names"] == ["token"] for i in insp.get_indexes("push_tokens")) or any(
        u["column_names"] == ["token"] for u in insp.get_unique_constraints("push_tokens"))

    _run(scratch, command.downgrade, PREVIOUS)
    assert "push_tokens" not in inspect(scratch).get_table_names()
    assert "visit_code_enc" not in _booking_columns(scratch)
    assert _enum_labels(scratch) == ["PENDING", "ACCEPTED", "IN_PROGRESS", "COMPLETED", "CANCELLED", "DECLINED"]

    _run(scratch, command.upgrade, THIS)  # and up again
    assert "EN_ROUTE" in _enum_labels(scratch)


def test_downgrade_turns_en_route_bookings_back_into_accepted_and_keeps_the_rest(scratch):
    _run(scratch, command.upgrade, THIS)
    en_route = _seed(scratch, BookingStatus.EN_ROUTE)
    in_progress = _seed(scratch, BookingStatus.IN_PROGRESS)

    _run(scratch, command.downgrade, PREVIOUS)
    with scratch.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, status::text FROM bookings")).all())
    assert rows[en_route] == "ACCEPTED"
    assert rows[in_progress] == "IN_PROGRESS"

    _run(scratch, command.upgrade, THIS)
    with scratch.connect() as conn:
        row = conn.execute(text(
            "SELECT visit_code_enc, visit_code_failed_attempts, visit_code_locked_until FROM bookings "
            "WHERE id = :i"), {"i": en_route}).one()
    assert row == (None, 0, None)  # existing rows get a zero counter and no code


def test_push_token_constraints(scratch):
    _run(scratch, command.upgrade, THIS)
    uid = _seed_user(scratch)
    with scratch.begin() as conn:
        conn.execute(text("INSERT INTO push_tokens (id, user_id, token, platform, created_at, last_seen_at) "
                          "VALUES (:i, :u, 'T1', 'ios', now(), now())"), {"i": uuid.uuid4(), "u": uid})
    for token, platform in (("T1", "ios"), ("T2", "blackberry")):  # duplicate token, bad platform
        with pytest.raises(DBAPIError):
            with scratch.begin() as conn:
                conn.execute(text("INSERT INTO push_tokens (id, user_id, token, platform, created_at, last_seen_at) "
                                  "VALUES (:i, :u, :t, :p, now(), now())"),
                             {"i": uuid.uuid4(), "u": uid, "t": token, "p": platform})
    # deleting the user takes their tokens with them
    with scratch.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        assert conn.execute(text("SELECT count(*) FROM push_tokens")).scalar() == 0


def _seed_user(eng):
    uid = uuid.uuid4()
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, full_name, hashed_password, user_type, is_active, is_verified) "
            "VALUES (:i, :e, 'U', 'x', 'patient', true, true)"), {"i": uid, "e": f"{uid.hex[:8]}@example.com"})
    return uid
