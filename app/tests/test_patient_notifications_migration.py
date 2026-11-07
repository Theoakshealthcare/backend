"""0021_patient_notifications: the ``notifications`` table with its feed indexes, the two new booking events, and a
downgrade that removes them again (and the rows that could not exist before).

Only touches its own revision and the previous one, on a scratch database.
"""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0021_patient_notifications", "0020_user_token_version"


def _booking(conn):
    conn.execute(text("INSERT INTO users (id, full_name, hashed_password, user_type) "
                      "VALUES ('00000000-0000-0000-0000-000000000001', 'Placeholder User', 'x', 'patient')"))
    conn.execute(text("INSERT INTO patients (id, user_id, full_name) VALUES "
                      "('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'P')"))
    conn.execute(text(
        "INSERT INTO bookings (id, booking_code, patient_id, service_type, assistance_type, duration_hours, "
        "scheduled_date, scheduled_end_date, patient_city, patient_state, status, payment_status, channel, "
        "total_amount_kobo, service_fee_kobo, platform_fee_kobo, booking_for) VALUES "
        "('00000000-0000-0000-0000-000000000003', 'MIG00001', '00000000-0000-0000-0000-000000000002', 'nurse', "
        "'home_visit', 4, now(), now(), 'Lagos', 'Lagos', 'PENDING', 'PENDING', 'app', 1000, 900, 100, 'myself')"))


def _event(conn, event):
    conn.execute(text("INSERT INTO booking_events (id, booking_id, event, created_at) VALUES "
                      f"(gen_random_uuid(), '00000000-0000-0000-0000-000000000003', '{event}', now())"))


def test_0021_follows_its_predecessor():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_adds_the_table_indexes_and_events_and_the_downgrade_removes_them(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    assert "notifications" not in inspect(scratch).get_table_names()
    with scratch.begin() as conn:
        _booking(conn)
    with pytest.raises(IntegrityError, match="ck_booking_events_event"), scratch.begin() as conn:
        _event(conn, "no_caregiver_found")  # not allowed before this revision

    _run(scratch, command.upgrade, THIS)
    insp = inspect(scratch)
    columns = {c["name"]: c for c in insp.get_columns("notifications")}
    assert set(columns) == {"id", "user_id", "type", "title", "body", "data", "read_at", "created_at", "event_id"}
    assert columns["read_at"]["nullable"] is True and columns["data"]["nullable"] is False
    assert {"ix_notifications_user_feed", "ix_notifications_user_unread"} <= {i["name"] for i in insp.get_indexes("notifications")}
    with scratch.begin() as conn:
        _event(conn, "caregiver_arrived")
        _event(conn, "no_caregiver_found")
        _event(conn, "visit_started")  # an event the old constraint allows survives the downgrade
        conn.execute(text("INSERT INTO notifications (id, user_id, type, title, body, created_at) VALUES "
                          "(gen_random_uuid(), '00000000-0000-0000-0000-000000000001', 'x', 't', 'b', now())"))

    _run(scratch, command.downgrade, PREVIOUS)
    assert "notifications" not in inspect(scratch).get_table_names()
    with scratch.connect() as conn:  # the rows that the old constraint would reject are gone, the table still works
        assert conn.execute(text("SELECT event FROM booking_events")).scalars().all() == ["visit_started"]
