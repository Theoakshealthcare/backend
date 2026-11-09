"""0026_service_catalog and 0027_booking_minutes: seed, backfill, constraints, and clean downgrades.

Each only touches its own revision and the previous one, on a scratch database.
"""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

BEFORE_CATALOG, SERVICE_CATALOG = "0025_visit_voice_notes", "0026_service_catalog"
BEFORE_MINUTES, MINUTES = "0026_service_catalog", "0027_booking_minutes"


def _booking(conn, n, hours):
    if n == 1:
        conn.execute(text("INSERT INTO users (id, full_name, hashed_password, user_type) "
                          "VALUES ('00000000-0000-0000-0000-000000000001', 'Placeholder User', 'x', 'patient')"))
        conn.execute(text("INSERT INTO patients (id, user_id, full_name) VALUES "
                          "('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'P')"))
    conn.execute(text(
        "INSERT INTO bookings (id, booking_code, patient_id, service_type, assistance_type, duration_hours, "
        "scheduled_date, scheduled_end_date, patient_city, patient_state, status, payment_status, channel, "
        "total_amount_kobo, service_fee_kobo, platform_fee_kobo, booking_for) VALUES "
        f"(gen_random_uuid(), 'MIG0000{n}', '00000000-0000-0000-0000-000000000002', 'nurse', "
        f"'home_visit', {hours}, now(), now(), 'Lagos', 'Lagos', 'PENDING', 'PENDING', 'app', 1000, 900, 100, 'myself')"))


def test_the_revisions_chain():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_revision(SERVICE_CATALOG).down_revision == BEFORE_CATALOG
    assert scripts.get_revision(MINUTES).down_revision == BEFORE_MINUTES


def test_0026_seeds_the_catalogue_without_prices_and_the_downgrade_drops_it(scratch):
    _run(scratch, command.upgrade, BEFORE_CATALOG)
    assert "service_catalog" not in inspect(scratch).get_table_names()
    _run(scratch, command.upgrade, SERVICE_CATALOG)
    with scratch.connect() as conn:
        rows = conn.execute(text("SELECT service_type, enabled, display_name, mode, min_minutes, step_minutes, "
                                 "max_minutes FROM service_catalog ORDER BY sort_order")).all()
    assert [tuple(r) for r in rows] == [
        ("nurse", True, "Nurse visit", "home", 60, 60, 1440),
        ("doctor", False, "Doctor visit", "home", 60, 60, 1440),
        ("physiotherapist", False, "Physiotherapy visit", "home", 60, 60, 1440),
        ("caregiver", False, "Caregiver visit", "home", 60, 60, 1440),
        ("nutritionist", False, "Nutrition consult", "home", 60, 60, 1440)]
    assert not {c["name"] for c in inspect(scratch).get_columns("service_catalog")} & {"price", "multiplier", "fee"}
    _run(scratch, command.downgrade, BEFORE_CATALOG)
    assert "service_catalog" not in inspect(scratch).get_table_names()


def test_0027_backfills_minutes_drops_hours_and_the_downgrade_restores_hours(scratch):
    _run(scratch, command.upgrade, BEFORE_MINUTES)
    with scratch.begin() as conn:
        _booking(conn, 1, 4)
        _booking(conn, 2, 1)
        conn.execute(text("UPDATE service_catalog SET step_minutes = 60"))

    _run(scratch, command.upgrade, MINUTES)
    cols = {c["name"]: c for c in inspect(scratch).get_columns("bookings")}
    assert "duration_hours" not in cols and cols["duration_minutes"]["nullable"] is False
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT booking_code, duration_minutes FROM bookings ORDER BY booking_code")).all() == [
            ("MIG00001", 240), ("MIG00002", 60)]
        catalog = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(text(
            "SELECT service_type, min_minutes, step_minutes, max_minutes FROM service_catalog"))}
    assert catalog["nutritionist"] == (30, 30, 1440) and catalog["nurse"] == (60, 30, 1440)
    with pytest.raises(IntegrityError, match="ck_bookings_duration_positive"), scratch.begin() as conn:
        conn.execute(text("UPDATE bookings SET duration_minutes = 0"))
    with scratch.begin() as conn:
        _booking_minutes(conn, 3, 90)  # a half-hour length can only exist now

    _run(scratch, command.downgrade, BEFORE_MINUTES)
    cols = {c["name"] for c in inspect(scratch).get_columns("bookings")}
    assert "duration_minutes" not in cols and "duration_hours" in cols
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT booking_code, duration_hours FROM bookings ORDER BY booking_code")).all() == [
            ("MIG00001", 4), ("MIG00002", 1), ("MIG00003", 2)]  # 90 minutes rounds UP to 2 hours
        assert conn.execute(text("SELECT min_minutes, step_minutes FROM service_catalog "
                                 "WHERE service_type = 'nutritionist'")).one() == (60, 60)
    _run(scratch, command.upgrade, MINUTES)  # and upgrades cleanly again
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT duration_minutes FROM bookings ORDER BY booking_code")).scalars().all() == [
            240, 60, 120]


def test_0027_downgrade_survives_a_catalogue_row_whose_min_and_max_share_a_part_hour(scratch):
    _run(scratch, command.upgrade, MINUTES)
    with scratch.begin() as conn:  # the admin schema accepts min = max = 90 at step 30, and min 45 / max 75 at step 15
        conn.execute(text("UPDATE service_catalog SET min_minutes = 90, max_minutes = 90 WHERE service_type = 'doctor'"))
        conn.execute(text("UPDATE service_catalog SET step_minutes = 15, min_minutes = 45, max_minutes = 75 "
                          "WHERE service_type = 'caregiver'"))
        conn.execute(text("UPDATE service_catalog SET min_minutes = 30, max_minutes = 30 "
                          "WHERE service_type = 'nutritionist'"))
        conn.execute(text("UPDATE service_catalog SET min_minutes = 90, max_minutes = 210 WHERE service_type = 'nurse'"))

    _run(scratch, command.downgrade, BEFORE_MINUTES)
    with scratch.connect() as conn:
        rows = {r[0]: tuple(r[1:]) for r in conn.execute(text(
            "SELECT service_type, min_minutes, step_minutes, max_minutes FROM service_catalog"))}
    assert rows["doctor"] == (120, 60, 120)
    assert rows["caregiver"] == (60, 60, 60)
    assert rows["nutritionist"] == (60, 60, 60)
    assert rows["nurse"] == (120, 60, 180)
    assert all(lo <= hi for lo, _, hi in rows.values())

    _run(scratch, command.upgrade, MINUTES)  # and upgrades cleanly again
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM service_catalog")).scalar() == 5


def _booking_minutes(conn, n, minutes):
    conn.execute(text(
        "INSERT INTO bookings (id, booking_code, patient_id, service_type, assistance_type, duration_minutes, "
        "scheduled_date, scheduled_end_date, patient_city, patient_state, status, payment_status, channel, "
        "total_amount_kobo, service_fee_kobo, platform_fee_kobo, booking_for) VALUES "
        f"(gen_random_uuid(), 'MIG0000{n}', '00000000-0000-0000-0000-000000000002', 'nurse', "
        f"'home_visit', {minutes}, now(), now(), 'Lagos', 'Lagos', 'PENDING', 'PENDING', 'app', 1000, 900, 100, 'myself')"))
