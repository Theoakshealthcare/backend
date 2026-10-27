"""0015_caregiver_kyc: the chain, the upgrade/downgrade round trip on a scratch database, and what the downgrade does to data the
old schema cannot hold. Constraint behaviour is covered by test_kyc_models.py."""
import uuid

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0015_caregiver_kyc", "0014_refunds_v2"
NEW_TABLES = {"identity_verifications", "kyc_webhook_events", "identity_verification_events"}
DROPPED = {"onfido_applicant_id", "onfido_check_id", "document_verification_result", "face_verification_result",
           "face_verification_status"}


def _columns(eng, table):
    return {c["name"] for c in inspect(eng).get_columns(table)}


def _constraint(eng, name) -> str:
    with eng.connect() as conn:
        return conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar()


def test_0015_follows_0014_and_is_the_head():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_revision(THIS).down_revision == PREVIOUS
    assert scripts.get_heads() == [THIS]


def test_upgrade_creates_the_tables_and_drops_the_columns(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    assert DROPPED <= _columns(scratch, "caregivers") and "onfido_document_id" in _columns(scratch, "caregiver_documents")
    _run(scratch, command.upgrade, THIS)
    assert NEW_TABLES <= set(inspect(scratch).get_table_names())
    assert not DROPPED & _columns(scratch, "caregivers")
    assert "onfido_document_id" not in _columns(scratch, "caregiver_documents")
    assert "needs_review" in _constraint(scratch, "ck_caregivers_identity_verification_status")
    assert "identity_verification" in _constraint(scratch, "ck_consents_type")
    indexes = {i["name"] for i in inspect(scratch).get_indexes("identity_verifications")}
    assert {"uq_identity_verifications_one_active", "ix_identity_verifications_fingerprint"} <= indexes


def test_upgrade_resets_an_unknown_identity_status_to_pending(scratch):  # noqa: F811
    _run(scratch, command.upgrade, PREVIOUS)
    uid, cid = uuid.uuid4(), uuid.uuid4()
    with scratch.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email, hashed_password, full_name, user_type, is_active, is_verified, "
                          "created_at, updated_at) VALUES (:u, 'a@example.com', 'x', 'A', 'caregiver', true, true, now(), now())"),
                     {"u": uid})
        conn.execute(text("INSERT INTO caregivers (id, user_id, email, full_name, identity_verification_status) "
                          "VALUES (:c, :u, 'a@example.com', 'A', 'approved-by-mistake')"), {"c": cid, "u": uid})
    _run(scratch, command.upgrade, THIS)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT identity_verification_status FROM caregivers WHERE id = :c"), {"c": cid}).scalar() == "pending"


def test_downgrade_restores_the_old_schema_and_maps_the_data(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    uid, cid = uuid.uuid4(), uuid.uuid4()
    with scratch.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email, hashed_password, full_name, user_type, is_active, is_verified, "
                          "created_at, updated_at) VALUES (:u, 'a@example.com', 'x', 'A', 'caregiver', true, true, now(), now())"),
                     {"u": uid})
        conn.execute(text("INSERT INTO caregivers (id, user_id, email, full_name, identity_verification_status) "
                          "VALUES (:c, :u, 'a@example.com', 'A', 'needs_review')"), {"c": cid, "u": uid})
        for kind in ("terms", "identity_verification"):
            conn.execute(text("INSERT INTO consents (id, subject_user_id, type, decision, policy_version, channel, created_at) "
                              "VALUES (:i, :u, :t, 'granted', 'v1', 'app', now())"), {"i": uuid.uuid4(), "u": uid, "t": kind})
    _run(scratch, command.downgrade, PREVIOUS)
    assert not NEW_TABLES & set(inspect(scratch).get_table_names())
    assert DROPPED <= _columns(scratch, "caregivers") and "onfido_document_id" in _columns(scratch, "caregiver_documents")
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT identity_verification_status FROM caregivers WHERE id = :c"), {"c": cid}).scalar() == "in_progress"
        assert [r[0] for r in conn.execute(text("SELECT type FROM consents"))] == ["terms"]
        # consents is append-only again after the one delete
        try:
            conn.execute(text("DELETE FROM consents"))
            raised = False
        except Exception as exc:  # DBAPIError
            raised = "append-only" in str(exc)
    assert raised
    assert "identity_verification" not in _constraint(scratch, "ck_consents_type")
    assert _constraint(scratch, "ck_caregivers_identity_verification_status") is None


def test_round_trip_leaves_the_trail_trigger_working(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    _run(scratch, command.downgrade, PREVIOUS)
    _run(scratch, command.upgrade, THIS)
    with scratch.connect() as conn:
        triggers = {r[0] for r in conn.execute(text(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'identity_verification_events'::regclass AND NOT tgisinternal"))}
    assert triggers == {"identity_verification_events_no_change", "identity_verification_events_no_truncate"}
