"""The KYC tables (identity_verifications, kyc_webhook_events, identity_verification_events) and the constraints that carry the
design's rules: the status set, one active attempt per caregiver, unique references and tokens, an append-only trail, the new
caregiver status and the new consent type. The suite database is built by ``alembic upgrade head``."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models.caregiver import Caregiver, CaregiverDocument
from app.db.models.consent import CONSENT_TYPES, Consent
from app.db.models.identity_verification import (
    IDENTITY_STATUSES, VERIFICATION_ACTIVE_STATUSES, VERIFICATION_STATUSES, VERIFICATION_TERMINAL_STATUSES,
    IdentityVerification, IdentityVerificationEvent, KycWebhookEvent,
)
from app.tests.factories import make_caregiver


def _attempt(caregiver, attempt_no=1, status="created", **kw):
    return IdentityVerification(caregiver_id=caregiver.id, attempt_no=attempt_no, status=status, **kw)


def _fails(db, *objs):
    db.add_all(objs)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.fixture
def cg(db):
    return make_caregiver(db, approved=False)


def test_status_sets():
    assert set(VERIFICATION_ACTIVE_STATUSES) == {"created", "started", "awaiting_result"}
    assert set(VERIFICATION_TERMINAL_STATUSES) == {"passed", "failed", "needs_review", "expired", "error"}
    assert set(VERIFICATION_STATUSES) == set(VERIFICATION_ACTIVE_STATUSES) | set(VERIFICATION_TERMINAL_STATUSES)
    assert set(IDENTITY_STATUSES) == {"pending", "in_progress", "needs_review", "verified", "failed"}


def test_defaults(db, cg):
    a = _attempt(cg)
    db.add(a)
    db.commit()
    db.refresh(a)
    assert isinstance(a.reference, uuid.UUID) and a.provider == "dojah" and a.reason_codes == []
    assert a.created_at is not None and a.updated_at is not None
    assert a.provider_session_id is None and a.id_fingerprint is None and a.decided_via is None


def test_array_of_reason_codes_round_trips(db, cg):
    a = _attempt(cg, status="needs_review", reason_codes=["name_mismatch", "face_grey_band"])
    db.add(a)
    db.commit()
    db.refresh(a)
    assert a.reason_codes == ["name_mismatch", "face_grey_band"]


@pytest.mark.parametrize("status", VERIFICATION_STATUSES)
def test_every_documented_status_is_accepted(db, cg, status):
    db.add(_attempt(cg, status=status))
    db.commit()


def test_unknown_status_is_refused(db, cg):
    _fails(db, _attempt(cg, status="verified"))


@pytest.mark.parametrize("field,value", [
    ("attempt_no", 0), ("id_type", "ssn"), ("decided_via", "robot"), ("face_confidence", 100.5), ("face_confidence", -1),
    ("liveness_confidence", 101), ("face_threshold", 101), ("face_threshold", -1), ("provider_cost", -1),
])
def test_value_checks(db, cg, field, value):
    _fails(db, _attempt(cg, **{field: value}))


@pytest.mark.parametrize("field,value", [
    ("attempt_no", 1), ("id_type", "nin"), ("id_type", "drivers_licence"), ("decided_via", "provider"), ("decided_via", "admin"),
    ("face_confidence", 0), ("face_confidence", 100), ("liveness_confidence", 87.25), ("face_threshold", 85),
    ("provider_cost", 620),
])
def test_value_checks_accept_the_edges(db, cg, field, value):
    db.add(_attempt(cg, **{field: value}))
    db.commit()


def test_one_active_attempt_per_caregiver(db, cg):
    db.add(_attempt(cg, 1, "created"))
    db.commit()
    for status in VERIFICATION_ACTIVE_STATUSES:
        _fails(db, _attempt(cg, 2, status))


def test_a_terminal_attempt_frees_the_slot_and_terminals_stack(db, cg):
    db.add_all([_attempt(cg, 1, "failed"), _attempt(cg, 2, "expired"), _attempt(cg, 3, "error")])
    db.commit()
    db.add(_attempt(cg, 4, "awaiting_result"))
    db.commit()


def test_other_caregivers_have_their_own_active_slot(db, cg):
    db.add_all([_attempt(cg), _attempt(make_caregiver(db, approved=False))])
    db.commit()


def test_attempt_numbers_are_unique_per_caregiver(db, cg):
    db.add(_attempt(cg, 1, "failed"))
    db.commit()
    _fails(db, _attempt(cg, 1, "passed"))


def test_reference_and_provider_session_are_globally_unique(db, cg):
    ref = uuid.uuid4()
    db.add(_attempt(cg, 1, "failed", reference=ref, provider_session_id="sdk_session_1"))
    db.commit()
    other = make_caregiver(db, approved=False)
    _fails(db, _attempt(other, 1, reference=ref))
    _fails(db, _attempt(other, 1, provider_session_id="sdk_session_1"))
    db.add_all([_attempt(other, 1, "failed"), _attempt(other, 2, "failed")])  # NULL session ids do not collide
    db.commit()


def test_fingerprint_is_indexed_not_unique(db, cg):
    other = make_caregiver(db, approved=False)
    db.add_all([_attempt(cg, 1, "passed", id_fingerprint="f" * 64), _attempt(other, 1, "needs_review", id_fingerprint="f" * 64)])
    db.commit()  # two accounts on one identity is the case the engine flags, so the schema must allow the rows


# --- webhook ledger --------------------------------------------------------------------------
def test_webhook_token_is_unique(db):
    db.add(KycWebhookEvent(token="tok-1", signature_ok=True))
    db.commit()
    _fails(db, KycWebhookEvent(token="tok-1", signature_ok=True))


def test_webhook_outcome_check_and_defaults(db, cg):
    ev = KycWebhookEvent(token="tok-2", signature_ok=True, provider_session_id="sdk_session_x")
    db.add(ev)
    db.commit()
    db.refresh(ev)
    assert ev.outcome is None and ev.processed_at is None and ev.received_at is not None and ev.verification_id is None
    for outcome in ("applied", "duplicate", "orphan", "ignored_terminal", "disagreement", "error"):
        ev.outcome = outcome
        db.commit()
    ev.outcome = "approved"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_the_ledger_stores_no_body_column():
    assert {c.name for c in KycWebhookEvent.__table__.columns} == {
        "id", "token", "provider_session_id", "event_type", "signature_ok", "body_sha256", "received_at", "processed_at",
        "outcome", "verification_id"}


def test_the_attempt_table_stores_nothing_the_provider_said_about_the_person():
    names = {c.name for c in IdentityVerification.__table__.columns}
    for forbidden in ("full_name", "name", "dob", "date_of_birth", "address", "photo", "image", "nin", "bvn", "id_number",
                      "number", "phone", "email", "payload", "body", "raw"):
        assert forbidden not in names, forbidden


# --- system-decision trail -------------------------------------------------------------------
def _event(attempt, **kw):
    fields = dict(verification_id=attempt.id, caregiver_id=attempt.caregiver_id, actor="provider", to_status="passed")
    fields.update(kw)
    return IdentityVerificationEvent(**fields)


@pytest.fixture
def attempt(db, cg):
    a = _attempt(cg)
    db.add(a)
    db.commit()
    return a


@pytest.mark.parametrize("actor", ["provider", "reconcile", "client", f"admin:{uuid.uuid4()}"])
def test_event_actors(db, attempt, actor):
    ev = _event(attempt, actor=actor, from_status="created", reason_codes=["face_mismatch"])
    db.add(ev)
    db.commit()
    db.refresh(ev)
    assert ev.at is not None and ev.reason_codes == ["face_mismatch"]


@pytest.mark.parametrize("kw", [{"actor": "robot"}, {"actor": "adminx"}, {"to_status": "verified"}, {"from_status": "verified"}])
def test_event_checks(db, attempt, kw):
    _fails(db, _event(attempt, **kw))


def test_events_are_append_only_in_the_orm(db, attempt):
    ev = _event(attempt)
    db.add(ev)
    db.commit()
    ev.to_status = "failed"
    with pytest.raises(RuntimeError, match="append-only"):
        db.commit()
    db.rollback()
    db.delete(ev)
    with pytest.raises(RuntimeError, match="append-only"):
        db.commit()
    db.rollback()


def test_events_are_append_only_in_the_database(db, attempt):
    db.add(_event(attempt))
    db.commit()
    for sql in ("UPDATE identity_verification_events SET to_status = 'failed'", "DELETE FROM identity_verification_events",
                "TRUNCATE identity_verification_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(sql))
        db.rollback()


# --- changes to existing tables --------------------------------------------------------------
@pytest.mark.parametrize("value", list(IDENTITY_STATUSES) + [None])
def test_caregiver_identity_status_accepts_the_documented_values(db, value):
    cg = make_caregiver(db, approved=False)
    cg.identity_verification_status = value
    db.commit()


@pytest.mark.parametrize("value", ["approved", "VERIFIED", "", "reviewing"])
def test_caregiver_identity_status_refuses_others(db, value):
    cg = make_caregiver(db, approved=False)
    cg.identity_verification_status = value
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_the_onfido_era_columns_are_gone():
    cols = {c.name for c in Caregiver.__table__.columns}
    assert not cols & {"onfido_applicant_id", "onfido_check_id", "document_verification_result", "face_verification_result",
                       "face_verification_status"}
    assert "onfido_document_id" not in {c.name for c in CaregiverDocument.__table__.columns}
    assert "extraction_data" in {c.name for c in CaregiverDocument.__table__.columns}  # OCR output stays


def test_consents_accept_the_identity_verification_type(db, cg):
    assert "identity_verification" in CONSENT_TYPES
    db.add(Consent(subject_user_id=cg.user_id, granted_by_user_id=cg.user_id, type="identity_verification", decision="granted",
                   policy_version="kyc-2026-10", channel="caregiver_app"))
    db.commit()
    db.add(Consent(subject_user_id=cg.user_id, type="biometrics", decision="granted", policy_version="v1", channel="x"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
