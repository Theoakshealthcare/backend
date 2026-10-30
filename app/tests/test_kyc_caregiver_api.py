"""Caregiver KYC API (K2): status, session creation, client hints, the outage path.

Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 6, 7, 10.2 and 12.1. The client is never trusted: nothing here may move
``caregivers.identity_verification_status`` to ``verified`` (or ``failed``), and a hint only touches the caregiver's own attempt.
"""
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func

from app.core.config import settings
from app.db.models.caregiver import Caregiver
from app.db.models.consent import Consent
from app.db.models.identity_verification import IdentityVerification, IdentityVerificationEvent
from app.tests.factories import auth_headers, make_caregiver, make_patient, refresh_headers
from app.tests.fake_dojah import (
    TEST_APP_ID, TEST_FINGERPRINT_KEY, TEST_PUBLIC_KEY, TEST_SECRET_KEY, TEST_WEBHOOK_SECRET, TEST_WIDGET_ID, new_reference,
)

BASE = "/api/v1/caregiver/identity-verification"
POLICY = "kyc-2026-10"
REFERENCE = re.compile(r"OKL-[0-9a-f]{32}")
STATE_KEYS = {"status", "attempts_used", "attempts_max", "active_verification_id", "last_attempt_status", "can_start",
              "failure_hint", "provider_available", "review_eta_hours", "policy_version"}


@pytest.fixture(autouse=True)
def env(fake_dojah, kyc_availability):
    """Dojah settings (every key is a placeholder from the fake, the public widget included) and a controllable availability flag."""
    return kyc_availability


def cg(db, **kw):
    kw.setdefault("profile_completion_step", 2)
    return make_caregiver(db, approved=False, **kw)


def attempt(db, caregiver, status="failed", **kw):
    n = (db.query(func.coalesce(func.max(IdentityVerification.attempt_no), 0))
         .filter(IdentityVerification.caregiver_id == caregiver.id).scalar()) + 1
    kw.setdefault("provider_session_id", new_reference())  # the reference the server minted at start
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=n, status=status, **kw)
    db.add(row)
    db.commit()
    return row


def get_state(client, caregiver):
    return client.get(BASE, headers=auth_headers(caregiver.user))


def start(client, caregiver, **body):
    body = {"consent": True, "policy_version": POLICY, **body}
    return client.post(f"{BASE}/sessions", json=body, headers=auth_headers(caregiver.user))


def hint(client, caregiver, verification_id, event, **extra):
    return client.post(f"{BASE}/{verification_id}/client-event", json={"event": event, **extra},
                       headers=auth_headers(caregiver.user))


def rows(db, model, **filters):
    db.expire_all()
    return db.query(model).filter_by(**filters).all()


def error(r):
    return r.json()["detail"]["code"]


# --- who may call -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body", [
    ("get", BASE, None),
    ("post", f"{BASE}/sessions", {"consent": True, "policy_version": POLICY}),
    ("post", f"{BASE}/{uuid.uuid4()}/client-event", {"event": "opened"}),
    ("post", f"{BASE}/defer", None),
])
def test_requires_a_token_and_a_caregiver(db, client, method, path, body):
    patient, caregiver = make_patient(db), cg(db)
    call = getattr(client, method)
    kwargs = {"json": body} if body is not None else {}
    assert call(path, **kwargs).status_code == 401
    assert call(path, headers=refresh_headers(caregiver.user), **kwargs).status_code == 401
    r = call(path, headers=auth_headers(patient.user), **kwargs)
    assert r.status_code == 403 and r.json()["detail"] == "Access denied. Caregiver account required."


# --- GET /identity-verification ---------------------------------------------------------------------------------------------
def test_a_fresh_caregiver_can_start(db, client):
    r = get_state(client, cg(db))
    assert r.status_code == 200
    assert r.json() == {"status": "pending", "attempts_used": 0, "attempts_max": 3, "active_verification_id": None,
                        "last_attempt_status": None, "can_start": True, "failure_hint": None, "provider_available": True,
                        "review_eta_hours": None, "policy_version": POLICY}


def test_cannot_start_before_step_two(db, client):
    assert get_state(client, cg(db, profile_completion_step=1)).json()["can_start"] is False


@pytest.mark.parametrize("identity,eta", [("verified", None), ("needs_review", 24), ("failed", None)])
def test_decided_or_in_review_caregivers_cannot_start(db, client, identity, eta):
    body = get_state(client, cg(db, identity_verification_status=identity)).json()
    assert body["status"] == identity and body["can_start"] is False and body["review_eta_hours"] == eta


def test_expired_and_error_attempts_do_not_count_and_the_cap_does(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    attempt(db, caregiver, "expired"), attempt(db, caregiver, "error"), attempt(db, caregiver, "failed")
    body = get_state(client, caregiver).json()
    assert body["attempts_used"] == 1 and body["can_start"] is True and body["last_attempt_status"] == "failed"
    attempt(db, caregiver, "failed"), attempt(db, caregiver, "failed")
    body = get_state(client, caregiver).json()
    assert body["attempts_used"] == 3 and body["can_start"] is False


def test_an_active_attempt_is_reported_and_still_startable(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    for _ in range(2):
        attempt(db, caregiver, "failed")
    active = attempt(db, caregiver, "awaiting_result")  # the third attempt is running: the cap is reached but it can be resumed
    body = get_state(client, caregiver).json()
    assert body["active_verification_id"] == str(active.id) and body["attempts_used"] == 3 and body["can_start"] is True
    assert body["last_attempt_status"] == "awaiting_result"


def test_provider_outage_is_reported_and_blocks_starting(db, client, env):
    env.available = False
    body = get_state(client, cg(db)).json()
    assert body["provider_available"] is False and body["can_start"] is False


@pytest.mark.parametrize("reasons,expected", [
    (["face_mismatch"], "lighting"), (["liveness_failed"], "lighting"), (["face_grey_band"], "lighting"),
    (["document_unreadable"], "document"), (["record_not_found"], "document"),
    (["provider_failed"], "mismatch_generic"), ([], "mismatch_generic"),
])
def test_failure_hint_is_a_coarse_enum_from_the_latest_clean_failure(db, client, reasons, expected):
    caregiver = cg(db, identity_verification_status="in_progress")
    attempt(db, caregiver, "failed", reason_codes=reasons, face_confidence=41, face_threshold=85, id_type="nin")
    body = get_state(client, caregiver).json()
    assert body["failure_hint"] == expected
    assert set(body) == STATE_KEYS


@pytest.mark.parametrize("reason", ["duplicate_identity", "watchlisted", "provider_disagreement", "name_mismatch",
                                    "dob_mismatch", "attempts_exhausted"])
def test_review_reasons_are_never_shown_to_the_caregiver(db, client, reason):
    caregiver = cg(db, identity_verification_status="needs_review")
    attempt(db, caregiver, "needs_review", reason_codes=[reason], face_confidence=61, face_threshold=85,
            provider_reference="placeholder-ref", id_fingerprint="placeholder-fingerprint")
    r = get_state(client, caregiver)
    assert r.json()["failure_hint"] is None
    text = r.text
    for leaked in (reason, "placeholder-ref", "placeholder-fingerprint", "face_confidence", "reason_codes", "61", "duplicate"):
        assert leaked not in text, leaked


def test_a_newer_attempt_replaces_the_old_hint(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    attempt(db, caregiver, "failed", reason_codes=["face_mismatch"])
    attempt(db, caregiver, "expired")
    assert get_state(client, caregiver).json()["failure_hint"] is None  # the latest attempt did not fail cleanly


# --- POST /identity-verification/sessions -----------------------------------------------------------------------------------
def test_a_session_is_created_with_widget_config_and_nothing_secret(db, client):
    caregiver = cg(db, full_name="Ada Obi Eze")
    r = start(client, caregiver)
    assert r.status_code == 201, r.text
    row = rows(db, IdentityVerification, caregiver_id=caregiver.id)[0]
    assert (row.status, row.attempt_no, row.provider) == ("created", 1, "dojah")
    body = r.json()
    assert set(body) == {"verification_id", "attempt_no", "widget", "state"}
    assert body["verification_id"] == str(row.id) and body["attempt_no"] == 1
    assert REFERENCE.fullmatch(row.provider_session_id)  # minted by the server BEFORE the widget opens, stored on the attempt
    assert row.provider_reference == row.provider_session_id  # what the admin console shows, to find the run in Dojah
    assert body["widget"] == {"app_id": TEST_APP_ID, "public_key": TEST_PUBLIC_KEY, "widget_id": TEST_WIDGET_ID,
                              "reference_id": row.provider_session_id, "email": caregiver.user.email, "first_name": "Ada",
                              "last_name": "Obi Eze",
                              "date_of_birth": caregiver.date_of_birth.isoformat() if caregiver.date_of_birth else None}
    assert body["state"]["active_verification_id"] == str(row.id) and body["state"]["status"] == "in_progress"
    for secret in (TEST_SECRET_KEY, TEST_WEBHOOK_SECRET, TEST_FINGERPRINT_KEY, str(row.reference)):
        assert secret not in r.text  # no secret, and not Oakly's own opaque attempt reference either


def test_every_attempt_gets_its_own_reference_and_a_resumed_attempt_keeps_it(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    first = start(client, caregiver)
    again = start(client, caregiver)
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["widget"]["reference_id"] == again.json()["widget"]["reference_id"]
    row = rows(db, IdentityVerification, caregiver_id=caregiver.id)[0]
    row.status = "failed"
    db.commit()
    second = start(client, caregiver)
    assert second.status_code == 201 and REFERENCE.fullmatch(second.json()["widget"]["reference_id"])
    assert second.json()["widget"]["reference_id"] != first.json()["widget"]["reference_id"]


def test_an_active_attempt_from_before_references_were_minted_gets_one_when_resumed(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    row = attempt(db, caregiver, "created", provider_session_id=None)
    r = start(client, caregiver)
    assert r.status_code == 200
    db.refresh(row)
    assert REFERENCE.fullmatch(row.provider_session_id) and r.json()["widget"]["reference_id"] == row.provider_session_id


def test_creating_a_session_records_the_separate_consent_trail_and_moves_to_in_progress(db, client):
    caregiver = cg(db)
    r = start(client, caregiver)
    row = rows(db, IdentityVerification, caregiver_id=caregiver.id)[0]
    consents = rows(db, Consent, subject_user_id=caregiver.user_id, type="identity_verification")
    assert len(consents) == 1
    c = consents[0]
    assert (c.decision, c.policy_version, c.channel, c.granted_by_user_id) == ("granted", POLICY, "caregiver_app", caregiver.user_id)
    assert c.evidence_ref == str(row.reference)
    events = rows(db, IdentityVerificationEvent, verification_id=row.id)
    assert [(e.actor, e.from_status, e.to_status) for e in events] == [("client", None, "created")]
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "in_progress" and caregiver.profile_completion_step == 2
    assert r.status_code == 201


def test_the_active_attempt_is_returned_not_duplicated(db, client):
    caregiver = cg(db)
    first = start(client, caregiver)
    second = start(client, caregiver)
    assert second.status_code == 200 and second.json() == first.json()
    assert len(rows(db, IdentityVerification, caregiver_id=caregiver.id)) == 1
    assert len(rows(db, Consent, subject_user_id=caregiver.user_id, type="identity_verification")) == 1


def test_parallel_requests_create_one_attempt(db, client):
    caregiver = cg(db)
    with ThreadPoolExecutor(4) as pool:
        codes = list(pool.map(lambda _: start(client, caregiver).status_code, range(4)))
    assert sorted(codes)[0] in (200, 201) and set(codes) <= {200, 201} and codes.count(201) == 1
    assert len(rows(db, IdentityVerification, caregiver_id=caregiver.id)) == 1


@pytest.mark.parametrize("body", [{"consent": False, "policy_version": POLICY}, {"policy_version": POLICY}, {"consent": True},
                                  {"consent": None, "policy_version": POLICY}, {}])
def test_consent_must_be_given_explicitly(db, client, body):
    caregiver = cg(db)
    r = client.post(f"{BASE}/sessions", json=body, headers=auth_headers(caregiver.user))
    assert r.status_code == 422
    assert rows(db, IdentityVerification, caregiver_id=caregiver.id) == []
    assert rows(db, Consent, subject_user_id=caregiver.user_id) == []
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "pending"


def test_a_stale_or_made_up_policy_version_is_refused(db, client):
    caregiver = cg(db)
    for version in ("kyc-2025-01", "", "anything"):
        r = start(client, caregiver, policy_version=version)
        assert r.status_code == 409 and error(r) == "POLICY_VERSION_MISMATCH"
    assert rows(db, Consent, subject_user_id=caregiver.user_id) == []


def test_the_attempt_cap_is_enforced_server_side(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    for _ in range(3):
        attempt(db, caregiver, "failed")
    r = start(client, caregiver)
    assert r.status_code == 409 and error(r) == "ATTEMPTS_EXHAUSTED"
    assert len(rows(db, IdentityVerification, caregiver_id=caregiver.id)) == 3


def test_expired_attempts_do_not_use_up_the_cap_and_attempt_numbers_keep_counting(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    for status in ("expired", "error", "expired", "failed"):
        attempt(db, caregiver, status)
    r = start(client, caregiver)
    assert r.status_code == 201 and r.json()["attempt_no"] == 5


def test_a_retry_after_a_failed_attempt_gets_a_new_reference_and_consent(db, client):
    caregiver = cg(db, identity_verification_status="in_progress")
    old = attempt(db, caregiver, "failed")
    r = start(client, caregiver)
    assert r.status_code == 201 and r.json()["attempt_no"] == 2
    assert r.json()["widget"]["reference_id"] != old.provider_session_id
    assert len(rows(db, Consent, subject_user_id=caregiver.user_id, type="identity_verification")) == 1
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "in_progress"


def test_other_caregivers_attempts_do_not_count(db, client):
    caregiver, other = cg(db), cg(db, identity_verification_status="in_progress")
    for _ in range(3):
        attempt(db, other, "failed")
    assert start(client, caregiver).status_code == 201


@pytest.mark.parametrize("identity,code", [("verified", "ALREADY_VERIFIED"), ("needs_review", "UNDER_REVIEW"),
                                           ("failed", "IDENTITY_REJECTED")])
def test_no_session_for_a_decided_or_in_review_caregiver(db, client, identity, code):
    caregiver = cg(db, identity_verification_status=identity)
    r = start(client, caregiver)
    assert r.status_code == 409 and error(r) == code
    assert rows(db, IdentityVerification, caregiver_id=caregiver.id) == []
    assert rows(db, Consent, subject_user_id=caregiver.user_id) == []
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == identity


def test_no_session_before_step_two(db, client):
    caregiver = cg(db, profile_completion_step=1)
    r = start(client, caregiver)
    assert r.status_code == 409 and error(r) == "PROFILE_INCOMPLETE"
    assert rows(db, IdentityVerification, caregiver_id=caregiver.id) == []


def test_provider_outage_refuses_a_session_without_side_effects(db, client, env):
    env.available = False
    caregiver = cg(db)
    r = start(client, caregiver)
    assert r.status_code == 503 and error(r) == "PROVIDER_UNAVAILABLE"
    assert rows(db, IdentityVerification, caregiver_id=caregiver.id) == []
    assert rows(db, Consent, subject_user_id=caregiver.user_id) == []
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "pending"


@pytest.mark.parametrize("setting", ["DOJAH_APP_ID", "DOJAH_PUBLIC_KEY", "DOJAH_WIDGET_ID"])
def test_an_unconfigured_widget_is_an_outage_not_a_half_session(db, client, monkeypatch, setting):
    monkeypatch.setattr(settings, setting, "")
    caregiver = cg(db)
    r = start(client, caregiver)
    assert r.status_code == 503 and error(r) == "PROVIDER_UNAVAILABLE"
    assert rows(db, IdentityVerification, caregiver_id=caregiver.id) == []


# --- POST /identity-verification/{id}/client-event --------------------------------------------------------------------------
def started(db, client):
    caregiver = cg(db)
    return caregiver, start(client, caregiver).json()["verification_id"]


def test_opened_then_completed_advance_the_attempt_and_never_touch_the_reference(db, client):
    caregiver, vid = started(db, client)
    reference = rows(db, IdentityVerification, id=uuid.UUID(vid))[0].provider_session_id
    r = hint(client, caregiver, vid, "opened")
    assert r.status_code == 200 and set(r.json()) == STATE_KEYS
    row = rows(db, IdentityVerification, id=uuid.UUID(vid))[0]
    assert row.status == "started" and row.started_at is not None
    r = hint(client, caregiver, vid, "completed")
    assert r.status_code == 200 and r.json()["last_attempt_status"] == "awaiting_result"
    row = rows(db, IdentityVerification, id=uuid.UUID(vid))[0]
    assert (row.status, row.provider_session_id) == ("awaiting_result", reference)
    steps = [(e.actor, e.from_status, e.to_status) for e in rows(db, IdentityVerificationEvent, verification_id=row.id)]
    assert ("client", "created", "started") in steps and ("client", "started", "awaiting_result") in steps


def test_completed_straight_from_created_is_accepted(db, client):
    caregiver, vid = started(db, client)
    assert hint(client, caregiver, vid, "completed").status_code == 200
    assert rows(db, IdentityVerification, id=uuid.UUID(vid))[0].status == "awaiting_result"


@pytest.mark.parametrize("event", ["closed", "error"])
def test_closed_and_error_do_not_change_or_consume_the_attempt(db, client, event):
    caregiver, vid = started(db, client)
    assert hint(client, caregiver, vid, event).status_code == 200
    assert rows(db, IdentityVerification, id=uuid.UUID(vid))[0].status == "created"
    assert get_state(client, caregiver).json()["attempts_used"] == 1
    assert start(client, caregiver).json()["verification_id"] == vid  # reopening the widget reuses the same attempt


def test_a_hint_never_moves_backwards(db, client):
    caregiver, vid = started(db, client)
    hint(client, caregiver, vid, "completed")
    hint(client, caregiver, vid, "opened")
    assert rows(db, IdentityVerification, id=uuid.UUID(vid))[0].status == "awaiting_result"


@pytest.mark.parametrize("terminal", ["passed", "failed", "needs_review", "expired", "error"])
def test_terminal_attempts_ignore_hints(db, client, terminal):
    caregiver = cg(db, identity_verification_status="in_progress")
    row = attempt(db, caregiver, terminal)
    reference = row.provider_session_id
    r = hint(client, caregiver, row.id, "completed")
    assert r.status_code == 200
    db.refresh(row)
    assert row.status == terminal and row.provider_session_id == reference
    assert rows(db, IdentityVerificationEvent, verification_id=row.id) == []


@pytest.mark.parametrize("identity", ["pending", "in_progress", "needs_review", "failed", "verified"])
@pytest.mark.parametrize("event", ["opened", "completed", "closed", "error"])
def test_a_hint_never_changes_the_caregiver(db, client, identity, event):
    caregiver = cg(db, identity_verification_status=identity)
    row = attempt(db, caregiver, "created")
    r = client.post(f"{BASE}/{row.id}/client-event", headers=auth_headers(caregiver.user),
                    json={"event": event, "session_id": "sess-1", "status": "verified", "verified": True,
                          "identity_verification_status": "verified", "result": {"overall_result": "passed"}})
    assert r.status_code == 200
    db.refresh(caregiver)
    assert (caregiver.identity_verification_status, caregiver.profile_completion_step) == (identity, 2)


def test_another_caregivers_attempt_is_a_404_and_untouched(db, client):
    mine, theirs = cg(db), cg(db, identity_verification_status="in_progress")
    row = attempt(db, theirs, "created")
    r = hint(client, mine, row.id, "completed")
    unknown = hint(client, mine, uuid.uuid4(), "completed")
    assert r.status_code == 404 and unknown.status_code == 404 and r.json() == unknown.json()  # no oracle on which ids exist
    db.refresh(row)
    assert row.status == "created"


def test_a_patient_cannot_post_a_hint_on_a_caregivers_attempt(db, client):
    caregiver, vid = started(db, client)
    r = client.post(f"{BASE}/{vid}/client-event", json={"event": "completed"}, headers=auth_headers(make_patient(db).user))
    assert r.status_code == 403
    assert rows(db, IdentityVerification, id=uuid.UUID(vid))[0].status == "created"


@pytest.mark.parametrize("field", ["session_id", "reference_id", "provider_session_id", "reference"])
@pytest.mark.parametrize("value", ["OKL-" + "a" * 32, "a/b", "../x", "has space", "x" * 500, "", "semi;colon"])
def test_a_reference_or_session_id_sent_by_the_client_is_ignored_never_stored(db, client, field, value):
    """The server alone chooses an attempt's provider reference: whatever the app claims is dropped, not even validated."""
    caregiver, vid = started(db, client)
    before = rows(db, IdentityVerification, id=uuid.UUID(vid))[0].provider_session_id
    r = client.post(f"{BASE}/{vid}/client-event", json={"event": "completed", field: value}, headers=auth_headers(caregiver.user))
    assert r.status_code == 200
    row = rows(db, IdentityVerification, id=uuid.UUID(vid))[0]
    assert row.status == "awaiting_result" and row.provider_session_id == before


def test_an_unknown_event_or_a_non_uuid_path_is_a_422(db, client):
    caregiver, vid = started(db, client)
    assert hint(client, caregiver, vid, "verified").status_code == 422
    assert hint(client, caregiver, "not-a-uuid", "opened").status_code == 422


# --- POST /identity-verification/defer --------------------------------------------------------------------------------------
def test_defer_during_an_outage_sets_step_three_and_verifies_nobody(db, client, env):
    env.available = False
    caregiver = cg(db)
    r = client.post(f"{BASE}/defer", headers=auth_headers(caregiver.user))
    assert r.status_code == 200 and set(r.json()) == STATE_KEYS
    db.refresh(caregiver)
    assert caregiver.profile_completion_step == 3 and caregiver.identity_verification_status == "pending"
    assert client.post(f"{BASE}/defer", headers=auth_headers(caregiver.user)).status_code == 200  # idempotent


def test_defer_keeps_an_in_progress_caregiver_in_progress(db, client, env):
    env.available = False
    caregiver = cg(db, identity_verification_status="in_progress")
    assert client.post(f"{BASE}/defer", headers=auth_headers(caregiver.user)).status_code == 200
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "in_progress" and caregiver.profile_completion_step == 3


def test_defer_is_refused_while_the_provider_is_available(db, client):
    caregiver = cg(db)
    r = client.post(f"{BASE}/defer", headers=auth_headers(caregiver.user))
    assert r.status_code == 409 and error(r) == "PROVIDER_AVAILABLE"
    db.refresh(caregiver)
    assert caregiver.profile_completion_step == 2


def test_defer_cannot_skip_onboarding_steps(db, client, env):
    env.available = False
    caregiver = cg(db, profile_completion_step=1)
    r = client.post(f"{BASE}/defer", headers=auth_headers(caregiver.user))
    assert r.status_code == 409 and error(r) == "PROFILE_INCOMPLETE"
    db.refresh(caregiver)
    assert caregiver.profile_completion_step == 1


@pytest.mark.parametrize("identity,code", [("verified", "ALREADY_VERIFIED"), ("needs_review", "UNDER_REVIEW"),
                                           ("failed", "IDENTITY_REJECTED")])
def test_defer_is_for_undecided_caregivers_only(db, client, env, identity, code):
    env.available = False
    caregiver = cg(db, identity_verification_status=identity)
    r = client.post(f"{BASE}/defer", headers=auth_headers(caregiver.user))
    assert r.status_code == 409 and error(r) == code
    db.refresh(caregiver)
    assert (caregiver.identity_verification_status, caregiver.profile_completion_step) == (identity, 2)


def test_a_deferred_caregiver_is_still_blocked_from_approval(db, client, env):
    from app.services.caregiver_approval import CaregiverApprovalService
    env.available = False
    caregiver = cg(db)
    client.post(f"{BASE}/defer", headers=auth_headers(caregiver.user))
    db.refresh(caregiver)
    unmet = CaregiverApprovalService(db).unmet_for_approval(caregiver)
    assert "IDENTITY_NOT_VERIFIED" in json.dumps(unmet, default=str)


# --- the old client-posted result ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("body", [{"type": "document", "result": {"status": "verified"}}, {"type": "face", "result": {}}, {},
                                  {"type": "face", "result": {"image": "data:image/png;base64,AAAA"}}])
def test_the_old_result_endpoint_is_gone(db, client, body):
    caregiver = cg(db)
    r = client.post(f"{BASE}/result", json=body, headers=auth_headers(caregiver.user))
    assert r.status_code == 410
    assert "verif" in r.json()["detail"].lower()
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "pending" and caregiver.profile_completion_step == 2  # not even in_progress


def test_the_old_result_endpoint_answers_410_without_a_body_and_keeps_its_guards(db, client):
    caregiver = cg(db)
    assert client.post(f"{BASE}/result", headers=auth_headers(caregiver.user)).status_code == 410
    assert client.post(f"{BASE}/result").status_code == 401
    assert client.post(f"{BASE}/result", headers=auth_headers(make_patient(db).user)).status_code == 403


def test_the_dead_code_is_deleted():
    from app.schemas import caregiver as schemas
    from app.services.caregiver import CaregiverService
    assert not hasattr(schemas, "VerificationResultRequest")
    assert not hasattr(CaregiverService, "process_verification_result")


# --- the trust property --------------------------------------------------------------------------------------------------
def test_no_sequence_of_caregiver_calls_can_verify_anyone(db, client, env):
    caregiver = cg(db)
    headers = auth_headers(caregiver.user)
    vid = start(client, caregiver).json()["verification_id"]
    claims = {"status": "verified", "verified": True, "identity_verification_status": "verified", "overall_result": "passed"}
    for event in ("opened", "completed", "closed", "error"):
        hint(client, caregiver, vid, event, session_id="sess-1", **claims)
        client.post(f"{BASE}/result", json={"type": "face", "result": claims}, headers=headers)
        env.available = False
        client.post(f"{BASE}/defer", headers=headers)
        env.available = True
        db.expire_all()
        assert db.get(Caregiver, caregiver.id).identity_verification_status in ("pending", "in_progress")
        assert db.get(IdentityVerification, uuid.UUID(vid)).status in ("created", "started", "awaiting_result")
