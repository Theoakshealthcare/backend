"""0035_care_alerts: two new tables, their constraints, and a clean round trip (scratch database)."""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

BEFORE, THIS = "0034_booking_reschedule", "0035_care_alerts"


def test_the_revision_chains_and_there_is_one_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision(THIS).down_revision == BEFORE
    assert len(script.get_heads()) == 1  # a later migration may sit on top; two heads would be a fork


def test_upgrade_creates_both_tables_with_their_checks_and_downgrade_drops_them(scratch):
    _run(scratch, command.upgrade, BEFORE)
    assert "care_alerts" not in inspect(scratch).get_table_names()

    _run(scratch, command.upgrade, THIS)
    names = inspect(scratch).get_table_names()
    assert {"care_alerts", "care_alert_occurrences"} <= set(names)
    columns = {c["name"] for c in inspect(scratch).get_columns("care_alerts")}
    assert {"type", "severity", "status", "patient_id", "care_recipient_id", "booking_id", "vital_id", "summary", "detail_encrypted",
            "occurrence_count", "last_occurred_at", "acknowledged_by_admin_id", "acknowledged_at", "resolved_by_admin_id",
            "resolved_at", "note_encrypted"} <= columns
    uniques = {u["name"] for u in inspect(scratch).get_unique_constraints("care_alert_occurrences")}
    assert "uq_care_alert_occurrences_source_key" in uniques

    with scratch.begin() as conn:  # a bad status, type or severity is refused by the database itself
        conn.execute(text("INSERT INTO users (id, full_name, hashed_password, user_type) VALUES (gen_random_uuid(), 'P', 'x', 'patient')"))
        conn.execute(text("INSERT INTO patients (id, user_id, full_name) SELECT gen_random_uuid(), id, 'P' FROM users"))
    for column, bad in (("type", "nonsense"), ("severity", "urgent"), ("status", "closed")):
        values = {"type": "task_missed", "severity": "warning", "status": "open", column: bad}
        try:
            with scratch.begin() as conn:
                conn.execute(text(
                    "INSERT INTO care_alerts (id, type, severity, status, patient_id, summary, last_occurred_at, created_at, updated_at) "
                    "SELECT gen_random_uuid(), :type, :severity, :status, id, 's', now(), now(), now() FROM patients"), values)
            raise AssertionError(f"{column}={bad} was accepted")
        except IntegrityError:
            pass

    _run(scratch, command.downgrade, BEFORE)
    names = inspect(scratch).get_table_names()
    assert "care_alerts" not in names and "care_alert_occurrences" not in names

    _run(scratch, command.upgrade, THIS)  # and it can go up again
    assert "care_alerts" in inspect(scratch).get_table_names()
