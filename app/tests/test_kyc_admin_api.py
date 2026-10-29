"""K5: the admin side of identity verification: the identity block on the caregiver detail, the queue filter and counts,
``identity-decision`` (closes the attempt, writes the event) and ``identity-reset`` (a fresh set of attempts).

Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 7.2, 12.3 and 13. All names and numbers are placeholders.
"""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models.audit import AdminAuditLog
from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import IdentityVerification, IdentityVerificationEvent
from app.services.kyc_caregiver import RESET_MARK, attempts_used
from app.tests.factories import auth_headers, make_admin, make_caregiver

BASE = "/api/v1/admin/caregivers"
KYC = "/api/v1/caregiver/identity-verification"
FINGERPRINT = "f" * 64  # placeholder keyed hash


def admin_headers(db, role="verification"):
    return auth_headers(make_admin(db, [role], password=None))


def cg(db, status="pending", **kw):
    kw.setdefault("profile_completion_step", 2)
    return make_caregiver(db, approved=False, identity_verification_status=status, **kw)


def attempt(db, caregiver, status, no=None, **kw):
    n = no or db.query(IdentityVerification).filter_by(caregiver_id=caregiver.id).count() + 1
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=n, status=status, **kw)
    db.add(row)
    db.commit()
    return row


def audit_rows(db, action=None):
    db.expire_all()
    rows = db.execute(select(AdminAuditLog).order_by(AdminAuditLog.created_at)).scalars().all()
    return [r for r in rows if action is None or r.action == action]


def events(db, caregiver):
    db.expire_all()
    return (db.query(IdentityVerificationEvent).filter_by(caregiver_id=caregiver.id)
            .order_by(IdentityVerificationEvent.at, IdentityVerificationEvent.id).all())


def identity_of(client, h, caregiver):
    r = client.get(f"{BASE}/{caregiver.id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["identity"]


# --- the identity block on the detail ----------------------------------------------------------------------------------
def test_detail_without_attempts_has_an_empty_identity_block(db, client):
    c = cg(db)
    identity = identity_of(client, admin_headers(db), c)
    assert identity == {
        "status": "pending", "review_reason": None, "attempts_used": 0, "attempts_max": settings.KYC_MAX_ATTEMPTS,
        "can_reset": False, "attempts": [], "duplicate_of_caregiver_id": None,
    }


def test_detail_lists_attempts_newest_first_with_the_spec_fields_and_no_legacy_keys(db, client):
    c = cg(db, "in_progress")
    old = attempt(db, c, "failed", reason_codes=["face_mismatch"], face_confidence=41.5, liveness_confidence=88, face_threshold=85,
                  name_match=True, dob_match=True, id_type="nin", provider_reference="placeholder-ref-1", decided_via="provider",
                  started_at=datetime(2026, 10, 1, 9, 0), completed_at=datetime(2026, 10, 1, 9, 5))
    new = attempt(db, c, "awaiting_result")
    identity = identity_of(client, admin_headers(db), c)

    assert [a["id"] for a in identity["attempts"]] == [str(new.id), str(old.id)]
    assert set(identity["attempts"][1]) == {
        "id", "attempt_no", "status", "created_at", "started_at", "completed_at", "id_type", "face_confidence",
        "liveness_confidence", "face_threshold", "name_match", "dob_match", "reason_codes", "provider_reference", "decided_via"}
    failed = identity["attempts"][1]
    assert (failed["attempt_no"], failed["status"], failed["id_type"]) == (1, "failed", "nin")
    assert failed["face_confidence"] == 41.5 and failed["liveness_confidence"] == 88.0 and failed["face_threshold"] == 85
    assert (failed["name_match"], failed["dob_match"]) == (True, True)
    assert failed["reason_codes"] == ["face_mismatch"] and failed["provider_reference"] == "placeholder-ref-1"
    assert failed["decided_via"] == "provider" and failed["completed_at"] is not None
    assert identity["attempts"][0]["face_confidence"] is None
    for legacy in ("document_verification_result", "face_verification_status", "face_verification_result"):
        assert legacy not in identity
    assert identity["attempts_used"] == 2 and identity["can_reset"] is False


def test_detail_never_carries_identity_numbers_images_session_ids_or_fingerprints(db, client):
    c = cg(db, "needs_review")
    attempt(db, c, "needs_review", reason_codes=["face_grey_band"], id_fingerprint=FINGERPRINT, id_type="bvn",
            provider_session_id="placeholder-session-secret", face_confidence=77)
    r = client.get(f"{BASE}/{c.id}", headers=admin_headers(db))
    text = json.dumps(r.json())
    assert FINGERPRINT not in text and "placeholder-session-secret" not in text
    identity = r.json()["identity"]
    assert not {"id_fingerprint", "provider_session_id", "number", "nin", "bvn", "image", "photo", "selfie"} & set(identity["attempts"][0])


@pytest.mark.parametrize("reasons,expected", [
    (["face_grey_band", "name_mismatch"], "face_grey_band"),
    (["name_mismatch", "duplicate_identity"], "duplicate_identity"),
    (["face_grey_band", "watchlisted"], "watchlisted"),
    (["provider_disagreement", "face_grey_band"], "provider_disagreement"),
    (["identity_data_missing"], "identity_data_missing"),
    (["provider_failed", "attempts_exhausted"], "attempts_exhausted"),
    (["something_new"], "something_new"),
    ([], None),
])
def test_review_reason_is_the_most_serious_reason_on_the_open_review_attempt(db, client, reasons, expected):
    c = cg(db, "needs_review")
    attempt(db, c, "failed", reason_codes=["face_mismatch"])
    attempt(db, c, "needs_review", reason_codes=reasons)
    assert identity_of(client, admin_headers(db), c)["review_reason"] == expected


def test_review_reason_is_null_unless_the_caregiver_is_waiting_for_review(db, client):
    c = cg(db, "failed")
    attempt(db, c, "needs_review", reason_codes=["watchlisted"])
    assert identity_of(client, admin_headers(db), c)["review_reason"] is None


def test_duplicate_of_names_the_other_caregiver_whose_attempt_passed_with_the_same_fingerprint(db, client):
    first, second, unrelated = cg(db, "verified"), cg(db, "needs_review"), cg(db, "verified")
    attempt(db, first, "passed", id_fingerprint=FINGERPRINT)
    attempt(db, unrelated, "passed", id_fingerprint="a" * 64)
    attempt(db, second, "needs_review", reason_codes=["duplicate_identity"], id_fingerprint=FINGERPRINT)
    h = admin_headers(db)
    assert identity_of(client, h, second)["duplicate_of_caregiver_id"] == str(first.id)
    assert identity_of(client, h, first)["duplicate_of_caregiver_id"] is None  # its own pass is not a duplicate
    assert identity_of(client, h, unrelated)["duplicate_of_caregiver_id"] is None


def test_a_fingerprint_on_an_unfinished_or_failed_attempt_is_not_a_duplicate(db, client):
    other, mine = cg(db), cg(db, "in_progress")
    attempt(db, other, "failed", id_fingerprint=FINGERPRINT)
    attempt(db, mine, "needs_review", id_fingerprint=FINGERPRINT)
    assert identity_of(client, admin_headers(db), mine)["duplicate_of_caregiver_id"] is None


def test_attempts_used_and_can_reset_follow_the_caregivers_state(db, client):
    c = cg(db, "failed")
    attempt(db, c, "failed")
    attempt(db, c, "expired")  # not counted
    attempt(db, c, "error")  # not counted
    identity = identity_of(client, admin_headers(db), c)
    assert identity["attempts_used"] == 1 and identity["can_reset"] is True
    for status, allowed in (("verified", False), ("pending", False), ("in_progress", False)):
        other = cg(db, status)
        assert identity_of(client, admin_headers(db), other)["can_reset"] is allowed
    # a review needs a verdict, unless it is only there because the attempts ran out
    exhausted, doubtful = cg(db, "needs_review"), cg(db, "needs_review")
    attempt(db, exhausted, "needs_review", reason_codes=["face_mismatch", "attempts_exhausted"])
    attempt(db, doubtful, "needs_review", reason_codes=["duplicate_identity"])
    assert identity_of(client, admin_headers(db), exhausted)["can_reset"] is True
    assert identity_of(client, admin_headers(db), doubtful)["can_reset"] is False


def test_reading_the_detail_is_still_an_audited_read(db, client):
    c = cg(db, "needs_review")
    attempt(db, c, "needs_review", id_fingerprint=FINGERPRINT)
    client.get(f"{BASE}/{c.id}", headers=admin_headers(db))
    (row,) = audit_rows(db, "caregiver.view")
    assert row.entity_id == str(c.id)


# --- the queue ---------------------------------------------------------------------------------------------------------
def queue(client, h, **params):
    params.setdefault("profile_status", "under_review")
    r = client.get(BASE, params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_queue_filters_on_identity_needs_review_and_counts_them(db, client):
    h = admin_headers(db)
    waiting = [cg(db, "needs_review", profile_status="under_review") for _ in range(2)]
    cg(db, "in_progress", profile_status="under_review")
    cg(db, "verified", profile_status="under_review")
    cg(db, "needs_review", profile_status="incomplete")  # another tab

    body = queue(client, h, identity="needs_review")
    assert {i["id"] for i in body["items"]} == {str(c.id) for c in waiting}
    assert body["total"] == 2
    assert body["needs_review_count"] == 2
    # the badge does not depend on the filter, but it follows the tab
    unfiltered = queue(client, h)
    assert unfiltered["total"] == 4 and unfiltered["needs_review_count"] == 2
    assert queue(client, h, profile_status="incomplete")["needs_review_count"] == 1


def test_the_queue_filter_works_with_paging(db, client):
    h = admin_headers(db)
    for _ in range(3):
        cg(db, "needs_review", profile_status="under_review")
    cg(db, "pending", profile_status="under_review")
    page = queue(client, h, identity="needs_review", limit=2, offset=2)
    assert page["total"] == 3 and len(page["items"]) == 1


def test_stuck_means_an_old_unfinished_attempt_of_a_caregiver_nobody_has_decided(db, client):
    h = admin_headers(db)
    old = datetime.utcnow() - timedelta(hours=settings.KYC_SESSION_TTL_HOURS + 3)
    stuck = cg(db, "in_progress", profile_status="under_review")
    attempt(db, stuck, "awaiting_result", created_at=old)
    fresh = cg(db, "in_progress", profile_status="under_review")
    attempt(db, fresh, "awaiting_result")  # just started
    just_over = cg(db, "in_progress", profile_status="under_review")
    attempt(db, just_over, "awaiting_result", created_at=datetime.utcnow() - timedelta(hours=settings.KYC_SESSION_TTL_HOURS, minutes=5))
    decided = cg(db, "verified", profile_status="under_review")
    attempt(db, decided, "created", created_at=old)  # an admin decided; the cron will expire this one
    closed = cg(db, "in_progress", profile_status="under_review")
    attempt(db, closed, "expired", created_at=old)

    body = queue(client, h, identity="stuck")
    assert [i["id"] for i in body["items"]] == [str(stuck.id)]
    assert body["stuck_count"] == 1 and body["total"] == 1


def test_the_failed_filter_lists_the_caregivers_an_admin_can_reset(db, client):
    h = admin_headers(db)
    failed = cg(db, "failed", profile_status="under_review")
    cg(db, "needs_review", profile_status="under_review")
    assert [i["id"] for i in queue(client, h, identity="failed")["items"]] == [str(failed.id)]


def test_an_unknown_identity_filter_is_a_422(db, client):
    assert client.get(BASE, params={"identity": "everything"}, headers=admin_headers(db)).status_code == 422


# --- identity-decision -------------------------------------------------------------------------------------------------
def decide(client, h, caregiver, decision, notes="Checked against the Prembly dashboard"):
    return client.post(f"{BASE}/{caregiver.id}/identity-decision", json={"decision": decision, "notes": notes}, headers=h)


def test_verifying_closes_the_review_attempt_and_writes_the_event(db, client):
    admin = make_admin(db, ["verification"], password=None)
    c = cg(db, "needs_review")
    older = attempt(db, c, "failed", decided_via="provider")
    review = attempt(db, c, "needs_review", reason_codes=["name_mismatch"], decided_via="provider")

    r = decide(client, auth_headers(admin), c, "verified")
    assert r.status_code == 200 and r.json()["identity_verification_status"] == "verified"
    db.expire_all()
    review, older = db.get(IdentityVerification, review.id), db.get(IdentityVerification, older.id)
    assert (review.status, review.decided_via) == ("passed", "admin")
    assert review.completed_at is not None and review.reason_codes == ["name_mismatch"]  # what the admin overrode stays visible
    assert (older.status, older.decided_via) == ("failed", "provider")  # history is untouched
    (event,) = events(db, c)
    assert (event.verification_id, event.actor) == (review.id, f"admin:{admin.id}")
    assert (event.from_status, event.to_status) == ("needs_review", "passed")
    (row,) = audit_rows(db, "caregiver.identity_decision")
    assert row.admin_user_id == admin.id and row.reason == "Checked against the Prembly dashboard"
    assert row.before["identity_verification_status"] == "needs_review" and row.after["identity_verification_status"] == "verified"
    assert row.after["identity_verification_id"] == str(review.id)


def test_failing_closes_the_review_attempt_as_failed_by_the_admin(db, client):
    c = cg(db, "needs_review")
    review = attempt(db, c, "needs_review", reason_codes=["duplicate_identity"])
    assert decide(client, admin_headers(db), c, "failed").status_code == 200
    db.expire_all()
    review = db.get(IdentityVerification, review.id)
    assert (review.status, review.decided_via) == ("failed", "admin")
    assert db.get(Caregiver, c.id).identity_verification_status == "failed"
    assert [(e.from_status, e.to_status) for e in events(db, c)] == [("needs_review", "failed")]


def test_deciding_closes_a_still_open_attempt_so_it_cannot_linger(db, client):
    c = cg(db, "in_progress")
    open_attempt = attempt(db, c, "awaiting_result")
    assert decide(client, admin_headers(db), c, "verified").status_code == 200
    db.expire_all()
    closed = db.get(IdentityVerification, open_attempt.id)
    assert closed.status == "expired" and closed.decided_via is None  # nothing was decided about the person by a provider
    (event,) = events(db, c)
    assert (event.from_status, event.to_status) == ("awaiting_result", "expired") and event.actor.startswith("admin:")
    assert attempts_used([closed]) == 0  # an expired attempt never counts


def test_an_admin_verdict_moves_a_stuck_onboarding_on_to_the_next_step_but_never_back(db, client):
    h = admin_headers(db)
    stuck = cg(db, "in_progress", profile_completion_step=2)
    ahead = cg(db, "needs_review", profile_completion_step=5)
    failing = cg(db, "in_progress", profile_completion_step=2)
    for c in (stuck, ahead):
        assert decide(client, h, c, "verified").status_code == 200
    assert decide(client, h, failing, "failed").status_code == 200
    db.expire_all()
    assert db.get(Caregiver, stuck.id).profile_completion_step == 3
    assert db.get(Caregiver, ahead.id).profile_completion_step == 5
    assert db.get(Caregiver, failing.id).profile_completion_step == 2


def test_a_decision_on_a_caregiver_without_attempts_still_works_and_is_audited(db, client):
    c = cg(db, "pending")
    assert decide(client, admin_headers(db), c, "verified").status_code == 200
    assert events(db, c) == []
    (row,) = audit_rows(db, "caregiver.identity_decision")
    assert row.after["identity_verification_id"] is None


def test_a_decision_on_a_case_that_moved_since_the_admin_looked_is_refused(db, client):
    h = admin_headers(db)
    c = cg(db, "in_progress")
    review = attempt(db, c, "needs_review", reason_codes=["watchlisted"])  # the worker landed this after the admin opened the page
    c.identity_verification_status = "needs_review"
    db.commit()
    url = f"{BASE}/{c.id}/identity-decision"
    body = {"decision": "verified", "notes": "x", "expected_status": "in_progress"}
    r = client.post(url, json=body, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["unmet"][0]["code"] == "IDENTITY_CASE_CHANGED"
    db.expire_all()
    assert db.get(Caregiver, c.id).identity_verification_status == "needs_review"
    assert db.get(IdentityVerification, review.id).status == "needs_review"
    assert audit_rows(db, "caregiver.identity_decision") == [] and events(db, c) == []
    assert client.post(url, json={**body, "expected_status": "needs_review"}, headers=h).status_code == 200
    assert client.post(url, json={**body, "expected_status": "bogus"}, headers=h).status_code == 422


def test_overriding_a_provider_pass_names_the_passed_attempt_in_the_audit_and_the_trail(db, client):
    admin = make_admin(db, ["verification"], password=None)
    c = cg(db, "verified")
    passed = attempt(db, c, "passed", decided_via="provider", id_fingerprint=FINGERPRINT)
    assert decide(client, auth_headers(admin), c, "failed").status_code == 200
    (row,) = audit_rows(db, "caregiver.identity_decision")
    assert row.after["identity_verification_id"] is None and row.after["overridden_verification_id"] == str(passed.id)
    (event,) = events(db, c)
    assert (event.verification_id, event.actor, event.from_status, event.to_status) == (passed.id, f"admin:{admin.id}", "passed", "passed")
    db.expire_all()
    assert db.get(IdentityVerification, passed.id).status == "passed"  # the evidence stays what it was


def test_the_decision_contract_is_unchanged(db, client):
    c = cg(db)
    h = admin_headers(db)
    url = f"{BASE}/{c.id}/identity-decision"
    assert client.post(url, json={"decision": "verified"}, headers=h).status_code == 422
    assert client.post(url, json={"decision": "verified", "notes": "  "}, headers=h).status_code == 422
    assert client.post(url, json={"decision": "cleared", "notes": "x"}, headers=h).status_code == 422
    assert client.post(f"{BASE}/{uuid.uuid4()}/identity-decision", json={"decision": "verified", "notes": "x"}, headers=h).status_code == 404
    assert audit_rows(db, "caregiver.identity_decision") == []


def test_a_late_provider_result_cannot_move_a_caregiver_an_admin_decided(db, client, fake_prembly):
    from app.db.models.identity_verification import KycWebhookEvent
    from app.services.kyc_events import process_event
    from app.services.kyc_hooks import KycHooks
    from app.tests.fake_prembly import FakeProviderAvailability

    c = cg(db, "in_progress", date_of_birth=datetime(1990, 4, 12).date())
    active = attempt(db, c, "awaiting_result")
    assert decide(client, admin_headers(db), c, "failed").status_code == 200
    ledger = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id="placeholder-session-late", signature_ok=True)
    db.add(ledger)
    db.commit()
    fake_prembly.script_clean_pass("placeholder-session-late", c)
    hooks = KycHooks(availability=FakeProviderAvailability(), notify=lambda *_: None, alert=lambda *_: None)
    process_event(db, {"event_id": str(ledger.id), "oakly_ref": str(active.reference)}, hooks=hooks)
    db.expire_all()
    assert db.get(Caregiver, c.id).identity_verification_status == "failed"
    assert db.get(IdentityVerification, active.id).status == "expired"


# --- identity-reset ----------------------------------------------------------------------------------------------------
def reset(client, h, caregiver, notes="Photo was blurry; allow another go"):
    return client.post(f"{BASE}/{caregiver.id}/identity-reset", json={"notes": notes}, headers=h)


def test_attempts_used_ignores_attempts_before_a_reset():
    def row(status, reasons=()):
        return IdentityVerification(status=status, reason_codes=list(reasons))
    assert attempts_used([row("failed"), row("failed", [RESET_MARK]), row("passed", ["x", RESET_MARK]), row("expired")]) == 1
    assert attempts_used([]) == 0


def test_reset_gives_a_failed_caregiver_a_fresh_set_of_attempts(db, client, monkeypatch, kyc_availability):
    monkeypatch.setattr(settings, "PREMBLY_WIDGET_ID", "placeholder-widget-id")
    monkeypatch.setattr(settings, "PREMBLY_WIDGET_KEY", "placeholder-widget-key")
    admin = make_admin(db, ["verification"], password=None)
    c = cg(db, "failed")
    for n in range(1, settings.KYC_MAX_ATTEMPTS + 1):
        attempt(db, c, "failed", no=n, reason_codes=["face_mismatch"])
    h = auth_headers(admin)
    mine = auth_headers(c.user)
    assert client.get(KYC, headers=mine).json()["attempts_used"] == settings.KYC_MAX_ATTEMPTS

    r = reset(client, h, c)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identity_verification_status"] == "pending"
    assert (body["attempts_used"], body["attempts_max"]) == (0, settings.KYC_MAX_ATTEMPTS)

    state = client.get(KYC, headers=mine).json()
    assert (state["status"], state["attempts_used"], state["can_start"], state["failure_hint"]) == ("pending", 0, True, None)
    assert state["last_attempt_status"] is None  # the wiped attempts are history, not what the app shows
    consent = {"consent": True, "policy_version": settings.KYC_POLICY_VERSION}
    started = client.post(f"{KYC}/sessions", json=consent, headers=mine)
    assert started.status_code == 201, started.text
    db.expire_all()
    numbers = sorted(a.attempt_no for a in db.query(IdentityVerification).filter_by(caregiver_id=c.id))
    assert numbers == [1, 2, 3, 4]  # numbering goes on; only the count starts again
    detail = client.get(f"{BASE}/{c.id}", headers=h).json()["identity"]
    assert detail["attempts_used"] == 1
    assert [RESET_MARK in a["reason_codes"] for a in detail["attempts"]] == [False, True, True, True]

    # and the fresh set is a real cap: after three more, the next is refused
    for _ in range(settings.KYC_MAX_ATTEMPTS - 1):
        db.query(IdentityVerification).filter_by(caregiver_id=c.id, status="created").update({"status": "failed"})
        db.commit()
        assert client.post(f"{KYC}/sessions", json=consent, headers=mine).status_code == 201
    db.query(IdentityVerification).filter_by(caregiver_id=c.id, status="created").update({"status": "failed"})
    db.commit()
    assert client.post(f"{KYC}/sessions", json=consent, headers=mine).status_code == 409


def test_reset_writes_the_audit_row_and_an_event_on_the_latest_attempt(db, client):
    admin = make_admin(db, ["verification"], password=None)
    c = cg(db, "failed")
    first = attempt(db, c, "failed")
    last = attempt(db, c, "failed")
    assert reset(client, auth_headers(admin), c).status_code == 200
    (row,) = audit_rows(db, "caregiver.identity_reset")
    assert row.admin_user_id == admin.id and row.entity_type == "caregiver" and row.entity_id == str(c.id)
    assert row.reason == "Photo was blurry; allow another go"
    assert row.before["identity_verification_status"] == "failed" and row.after["identity_verification_status"] == "pending"
    assert sorted(row.after["attempts_reset"]) == sorted([str(first.id), str(last.id)])
    (event,) = events(db, c)
    assert (event.verification_id, event.actor) == (last.id, f"admin:{admin.id}")
    assert (event.from_status, event.to_status, event.reason_codes) == ("failed", "failed", [RESET_MARK])


def test_reset_of_a_caregiver_waiting_for_review_hands_the_case_back(db, client):
    c = cg(db, "needs_review")
    review = attempt(db, c, "needs_review", reason_codes=["attempts_exhausted"])
    assert reset(client, admin_headers(db), c).status_code == 200
    db.expire_all()
    assert db.get(Caregiver, c.id).identity_verification_status == "pending"
    assert db.get(IdentityVerification, review.id).status == "needs_review"  # history keeps what happened
    # the old review attempt is no longer "open": a later decision must not pick it up
    again = cg(db, "needs_review")
    old = attempt(db, again, "needs_review", reason_codes=[RESET_MARK, "face_grey_band"])
    fresh = attempt(db, again, "needs_review", reason_codes=["watchlisted"])
    identity = identity_of(client, admin_headers(db), again)
    assert identity["review_reason"] == "watchlisted"
    assert decide(client, admin_headers(db), again, "verified").status_code == 200
    db.expire_all()
    assert db.get(IdentityVerification, fresh.id).status == "passed"
    assert db.get(IdentityVerification, old.id).status == "needs_review"


def test_reset_closes_an_open_attempt_and_does_not_touch_the_profile_step(db, client):
    c = cg(db, "failed", profile_completion_step=4)
    open_attempt = attempt(db, c, "started")
    assert reset(client, admin_headers(db), c).status_code == 200
    db.expire_all()
    assert db.get(IdentityVerification, open_attempt.id).status == "expired"
    assert db.get(Caregiver, c.id).profile_completion_step == 4
    assert [(e.from_status, e.to_status) for e in events(db, c)] == [("started", "expired"), ("expired", "expired")]


@pytest.mark.parametrize("status", ["pending", "in_progress", "verified", "needs_review"])
def test_reset_is_refused_unless_the_caregiver_failed_or_ran_out_of_attempts(db, client, status):
    c = cg(db, status)
    attempt(db, c, "failed")  # for needs_review: no exhausted review attempt, e.g. a doubtful case that needs a verdict
    r = reset(client, admin_headers(db), c)
    assert r.status_code == 409
    assert r.json()["detail"]["unmet"][0]["code"] == "IDENTITY_RESET_NOT_ALLOWED"
    db.expire_all()
    assert db.get(Caregiver, c.id).identity_verification_status == status
    assert audit_rows(db, "caregiver.identity_reset") == [] and events(db, c) == []
    assert RESET_MARK not in db.query(IdentityVerification).filter_by(caregiver_id=c.id).one().reason_codes


def test_a_doubtful_review_cannot_be_reset_away_but_can_be_failed_then_reset(db, client):
    h = admin_headers(db)
    c = cg(db, "needs_review")
    attempt(db, c, "needs_review", reason_codes=["watchlisted"])
    assert reset(client, h, c).status_code == 409
    assert decide(client, h, c, "failed").status_code == 200
    assert reset(client, h, c).status_code == 200


def test_reset_validates_notes_and_the_caregiver(db, client):
    h = admin_headers(db)
    c = cg(db, "failed")
    url = f"{BASE}/{c.id}/identity-reset"
    assert client.post(url, json={}, headers=h).status_code == 422
    assert client.post(url, json={"notes": "   "}, headers=h).status_code == 422
    assert client.post(url, json={"notes": "x", "decision": "verified"}, headers=h).status_code == 422
    assert client.post(f"{BASE}/{uuid.uuid4()}/identity-reset", json={"notes": "x"}, headers=h).status_code == 404
    assert audit_rows(db, "caregiver.identity_reset") == []


def test_reset_does_not_reinstate_or_approve_anyone(db, client):
    c = cg(db, "failed", profile_status="suspended", suspension_reason="identity failed after approval")
    assert reset(client, admin_headers(db), c).status_code == 200
    db.expire_all()
    fresh = db.get(Caregiver, c.id)
    assert fresh.profile_status == "suspended" and fresh.suspension_reason == "identity failed after approval"


def test_two_resets_at_once_have_one_winner(db, client):
    h = admin_headers(db)
    for _ in range(3):
        c = cg(db, "failed")
        attempt(db, c, "failed")
        cid = c.id
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(lambda: client.post(f"{BASE}/{cid}/identity-reset", json={"notes": "one"}, headers=h).status_code)
            b = pool.submit(lambda: client.post(f"{BASE}/{cid}/identity-reset", json={"notes": "two"}, headers=h).status_code)
            assert sorted([a.result(), b.result()]) == [200, 409]
        assert len([r for r in audit_rows(db, "caregiver.identity_reset") if r.entity_id == str(cid)]) == 1


# --- permissions (design 12.3: no new permission strings; Finance has neither) -------------------------------------------
@pytest.mark.parametrize("role", ["finance", "operations", "support", "clinical_reviewer", "read_only_analyst"])
def test_roles_without_caregivers_approve_can_neither_decide_nor_reset(db, client, role):
    h = admin_headers(db, role)
    c = cg(db, "needs_review")
    review = attempt(db, c, "needs_review")
    for r in (decide(client, h, c, "verified"), reset(client, h, c)):
        assert r.status_code == 403
        assert "caregivers.approve" in r.json()["detail"]
    db.expire_all()
    assert db.get(Caregiver, c.id).identity_verification_status == "needs_review"
    assert db.get(IdentityVerification, review.id).status == "needs_review"
    assert audit_rows(db, "caregiver.identity_decision") == [] and audit_rows(db, "caregiver.identity_reset") == []
    assert events(db, c) == []


@pytest.mark.parametrize("role", ["verification", "super_admin"])
def test_the_roles_that_approve_caregivers_can_decide_and_reset(db, client, role):
    h = admin_headers(db, role)
    assert decide(client, h, cg(db, "needs_review"), "failed").status_code == 200
    assert reset(client, h, cg(db, "failed")).status_code == 200


def test_the_identity_endpoints_need_a_token(db, client):
    c = cg(db, "failed")
    assert client.post(f"{BASE}/{c.id}/identity-reset", json={"notes": "x"}).status_code == 401
    assert client.post(f"{BASE}/{c.id}/identity-decision", json={"decision": "verified", "notes": "x"}).status_code == 401
    assert client.get(f"{BASE}/{c.id}").status_code == 401
    assert client.get(BASE, params={"identity": "needs_review"}).status_code == 401


def test_reading_needs_caregivers_read_only(db, client):
    c = cg(db, "needs_review")
    assert client.get(f"{BASE}/{c.id}", headers=admin_headers(db, "read_only_analyst")).status_code == 403
    assert client.get(BASE, params={"identity": "needs_review"}, headers=admin_headers(db, "read_only_analyst")).status_code == 403
    for role in ("finance", "support", "operations"):  # they hold caregivers.read, as for the rest of the detail
        assert client.get(f"{BASE}/{c.id}", headers=admin_headers(db, role)).status_code == 200


def test_a_caregiver_token_cannot_reach_the_admin_identity_routes(db, client):
    c = cg(db, "failed")
    h = auth_headers(c.user)
    assert client.post(f"{BASE}/{c.id}/identity-reset", json={"notes": "x"}, headers=h).status_code in (401, 403)
    assert client.post(f"{BASE}/{c.id}/identity-decision", json={"decision": "verified", "notes": "x"}, headers=h).status_code in (401, 403)
    db.expire_all()
    assert db.get(Caregiver, c.id).identity_verification_status == "failed"
