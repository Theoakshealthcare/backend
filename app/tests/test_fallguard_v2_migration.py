"""0033_fallguard_v2_results: nullable score, the version/tier/sealed columns, the immutability trigger, a clean downgrade."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

BEFORE, REVISION = "0031_care_templates_docs_space", "0033_fallguard_v2_results"
USER, PATIENT, V1_ROW = ("00000000-0000-0000-0000-00000000f001", "00000000-0000-0000-0000-00000000f002",
                         "00000000-0000-0000-0000-00000000f003")
NEW = {"questionnaire_version", "algorithm_version", "content_version", "eligible", "gate_tier", "score_tier", "final_tier",
       "urgent", "incomplete", "answers_encrypted", "result_encrypted"}


def _columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns("fallguard_assessments")}


def _v1_row(conn):
    conn.execute(text("INSERT INTO users (id, full_name, hashed_password, user_type) "
                      f"VALUES ('{USER}', 'Placeholder User', 'x', 'patient')"))
    conn.execute(text(f"INSERT INTO patients (id, user_id, full_name) VALUES ('{PATIENT}', '{USER}', 'P')"))
    conn.execute(text(f"INSERT INTO fallguard_assessments (id, patient_id, total_score) VALUES ('{V1_ROW}', '{PATIENT}', 7)"))


def test_the_revision_chains_on_0031():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(REVISION).down_revision == BEFORE


def test_upgrade_keeps_a_v1_row_and_makes_the_new_columns_null(scratch):
    _run(scratch, command.upgrade, BEFORE)
    assert _columns(scratch)["total_score"]["nullable"] is False and not (NEW & set(_columns(scratch)))
    with scratch.begin() as conn:
        _v1_row(conn)
    _run(scratch, command.upgrade, REVISION)
    cols = _columns(scratch)
    assert NEW <= set(cols) and cols["total_score"]["nullable"] is True
    with scratch.connect() as conn:
        row = conn.execute(text(f"SELECT total_score, final_tier, answers_encrypted, urgent FROM fallguard_assessments "
                                f"WHERE id = '{V1_ROW}'")).one()
    assert tuple(row) == (7, None, None, None)


def test_the_check_constraint_and_the_immutability_trigger(scratch):
    _run(scratch, command.upgrade, REVISION)
    with scratch.begin() as conn:
        _v1_row(conn)
        conn.execute(text("UPDATE fallguard_assessments SET final_tier = NULL"))  # a v1 row's own NULLs are untouched: allowed
    with pytest.raises(IntegrityError), scratch.begin() as conn:
        conn.execute(text("INSERT INTO fallguard_assessments (id, patient_id, final_tier) "
                          f"VALUES (gen_random_uuid(), '{PATIENT}', 'EXTREME')"))
    with scratch.begin() as conn:
        conn.execute(text("INSERT INTO fallguard_assessments (id, patient_id, total_score, final_tier, urgent) "
                          f"VALUES ('00000000-0000-0000-0000-00000000f004', '{PATIENT}', NULL, NULL, false)"))
    with pytest.raises(DBAPIError, match="immutable"), scratch.begin() as conn:
        conn.execute(text("UPDATE fallguard_assessments SET urgent = true "
                          "WHERE id = '00000000-0000-0000-0000-00000000f004'"))
    with scratch.begin() as conn:  # ciphertext and the AI columns may change (key rotation, the late AI summary)
        conn.execute(text("UPDATE fallguard_assessments SET answers_encrypted = 'x', ai_summary_encrypted = 'y' "
                          "WHERE id = '00000000-0000-0000-0000-00000000f004'"))


def test_downgrade_drops_the_columns_and_the_trigger_and_restores_not_null(scratch):
    _run(scratch, command.upgrade, REVISION)
    with scratch.begin() as conn:
        _v1_row(conn)
        conn.execute(text("INSERT INTO fallguard_assessments (id, patient_id, total_score) VALUES "
                          f"('00000000-0000-0000-0000-00000000f004', '{PATIENT}', NULL)"))
    _run(scratch, command.downgrade, BEFORE)
    cols = _columns(scratch)
    assert not (NEW & set(cols)) and cols["total_score"]["nullable"] is False
    with scratch.connect() as conn:
        scores = [r[0] for r in conn.execute(text("SELECT total_score FROM fallguard_assessments ORDER BY total_score"))]
        trigger = conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_fallguard_result_immutable'")).scalar()
    assert scores == [0, 7] and trigger == 0
    _run(scratch, command.upgrade, REVISION)  # and it goes up again
