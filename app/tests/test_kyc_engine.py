"""K4: the decision engine against the database. A pull-confirmed result becomes an attempt outcome, a caregiver identity status,
an append-only event row and a notice, in one locked section; nothing a provider says can overwrite a human decision.

The pure rules are in test_kyc_decision.py; the lock-order proofs for the worker are in test_kyc_events.py; the cron is in
test_kyc_reconcile.py. Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 7 and 16.1. All names and numbers are placeholders.
"""
import logging
import re
import threading
from types import SimpleNamespace
import time
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import IdentityVerification, IdentityVerificationEvent, KycWebhookEvent
from app.db.session import SessionLocal
from app.services import kyc_notify
from app.services.kyc_caregiver import CaregiverIdentityService
from app.services.kyc_events import process_event
from app.services.kyc_hooks import KycHooks
from app.tests.factories import make_caregiver
from app.tests.fake_prembly import FakeProviderAvailability, SANDBOX_BVN, SANDBOX_NIN, fake_prembly  # noqa: F401


@pytest.fixture(autouse=True)
def env(fake_prembly):  # noqa: F811
    return fake_prembly


class Recorder:
    """Stands in for push + e-mail + the ops alert: remembers what would have been sent."""

    def __init__(self):
        self.notices, self.alerts = [], []
        self.availability = FakeProviderAvailability()

    def hooks(self) -> KycHooks:
        return KycHooks(availability=self.availability, notify=lambda db, n: self.notices.append(n), alert=self.alerts.append)

    @property
    def kinds(self):
        return [n.kind for n in self.notices]


@pytest.fixture
def rec():
    return Recorder()


def cg(db, **kw):
    kw.setdefault("date_of_birth", date(1990, 4, 12))
    kw.setdefault("profile_completion_step", 2)
    kw.setdefault("identity_verification_status", "in_progress")
    return make_caregiver(db, approved=False, **kw)


def attempt(db, caregiver, status="awaiting_result", **kw):
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=kw.pop("attempt_no", 1), status=status, **kw)
    db.add(row)
    db.commit()
    return row


def fresh(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


def run(db, rec, session_id, a, *, claim=None):
    """One webhook event through the worker (the ledger row stands in for the endpoint's insert)."""
    event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id=session_id, signature_ok=True)
    db.add(event)
    db.commit()
    job = {"event_id": str(event.id), "oakly_ref": str(a.reference), "claim": claim}
    return process_event(db, job, hooks=rec.hooks())


# --- the decision matrix, end to end ---------------------------------------------------------------------------------
def test_a_clean_pass_verifies_the_caregiver_and_tells_them(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    result = run(db, rec, "sdk_session_a", a)
    assert result.outcome == "applied"
    c, a = fresh(db, caregiver), fresh(db, a)
    assert (c.identity_verification_status, c.profile_completion_step) == ("verified", 3)
    assert (a.status, a.decided_via, a.reason_codes) == ("passed", "provider", [])
    assert rec.kinds == ["verified"] and rec.notices[0].user_id == caregiver.user_id
    (trail,) = db.query(IdentityVerificationEvent).filter_by(verification_id=a.id).all()
    assert (trail.actor, trail.to_status) == ("provider", "passed")


def test_a_clean_failure_asks_for_another_try_and_never_fails_the_caregiver(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="failed",
                                failure_reason="Unable to read the document", face_confidence=None)
    run(db, rec, "sdk_session_a", a)
    c, a = fresh(db, caregiver), fresh(db, a)
    assert (c.identity_verification_status, c.profile_completion_step) == ("in_progress", 2)  # still on the identity step
    assert (a.status, a.reason_codes) == ("failed", ["document_unreadable"])
    assert rec.kinds == ["try_again"]
    state = CaregiverIdentityService(db, FakeProviderAvailability()).state(c)
    assert state["failure_hint"] == "document" and state["can_start"] is True and state["attempts_used"] == 1


def test_three_clean_failures_end_in_review_not_in_failed(db, rec, fake_prembly):
    caregiver = cg(db)
    kinds, statuses = [], []
    for n in (1, 2, 3):
        a = attempt(db, caregiver, attempt_no=n)
        fake_prembly.script_session(f"sdk_session_{n}", email=caregiver.email, overall_result="failed", face_confidence=20)
        run(db, rec, f"sdk_session_{n}", a)
        statuses.append((fresh(db, a).status, fresh(db, caregiver).identity_verification_status))
    assert statuses == [("failed", "in_progress"), ("failed", "in_progress"), ("needs_review", "needs_review")]
    assert rec.kinds == ["try_again", "try_again", "under_review"]
    last = db.query(IdentityVerification).filter_by(attempt_no=3, caregiver_id=caregiver.id).one()
    assert last.reason_codes == ["face_mismatch", "attempts_exhausted"]
    assert fresh(db, caregiver).profile_completion_step == 3  # handed to a person: the app moves on to consent


def test_expired_and_error_attempts_do_not_count_toward_exhaustion(db, rec, fake_prembly):
    caregiver = cg(db)
    attempt(db, caregiver, status="expired", attempt_no=1)
    attempt(db, caregiver, status="error", attempt_no=2)
    attempt(db, caregiver, status="failed", attempt_no=3)
    a = attempt(db, caregiver, attempt_no=4)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="failed", face_confidence=20)
    run(db, rec, "sdk_session_a", a)
    assert (fresh(db, a).status, fresh(db, caregiver).identity_verification_status) == ("failed", "in_progress")  # the 2nd counted failure


@pytest.mark.parametrize("override,reason,name,dob", [
    (dict(registry_name="CHINEDU OKAFOR"), "name_mismatch", False, True),
    (dict(registry_dob="1991-04-12"), "dob_mismatch", True, False),
    (dict(face_confidence=78.0), "face_grey_band", True, True),
    (dict(registry_name=None), "identity_data_missing", None, True),
    (dict(face_confidence=None), "identity_data_missing", True, True),
])
def test_ambiguous_results_go_to_the_review_queue_never_to_rejection(db, rec, fake_prembly, override, reason, name, dob):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, **override)
    run(db, rec, "sdk_session_a", a)
    c, a = fresh(db, caregiver), fresh(db, a)
    assert (c.identity_verification_status, a.status, a.reason_codes) == ("needs_review", "needs_review", [reason])
    assert (a.name_match, a.dob_match) == (name, dob)
    assert rec.kinds == ["under_review"]


def test_a_watch_listed_identity_goes_to_review_whatever_the_verdict(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, watchlisted=True)
    run(db, rec, "sdk_session_a", a)
    assert (fresh(db, caregiver).identity_verification_status, fresh(db, a).reason_codes) == ("needs_review", ["watchlisted"])


def test_a_provider_that_passes_a_low_face_score_is_still_a_failure(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, face_confidence=40.0)
    run(db, rec, "sdk_session_a", a)
    assert (fresh(db, a).status, fresh(db, a).reason_codes, fresh(db, caregiver).identity_verification_status) == (
        "failed", ["face_mismatch"], "in_progress")


def test_the_thresholds_come_from_settings(db, rec, fake_prembly, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "KYC_FACE_THRESHOLD", 95)
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, face_confidence=92.0)
    run(db, rec, "sdk_session_a", a)
    assert (fresh(db, a).reason_codes, fresh(db, a).face_threshold) == (["face_grey_band"], 95)


def test_a_caregiver_past_step_three_is_not_moved_back(db, rec, fake_prembly):
    caregiver = cg(db, profile_completion_step=4)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    run(db, rec, "sdk_session_a", a)
    assert fresh(db, caregiver).profile_completion_step == 4


# --- duplicate identity ----------------------------------------------------------------------------------------------
def test_one_nin_on_two_accounts_sends_the_second_to_review_without_saying_why(db, rec, fake_prembly):
    first, second = cg(db), cg(db)
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_prembly.script_clean_pass("sdk_session_1", first)
    fake_prembly.script_clean_pass("sdk_session_2", second)
    run(db, rec, "sdk_session_1", a1)
    run(db, rec, "sdk_session_2", a2)
    assert fresh(db, first).identity_verification_status == "verified"
    c2, a2 = fresh(db, second), fresh(db, a2)
    assert (c2.identity_verification_status, a2.status, a2.reason_codes) == ("needs_review", "needs_review", ["duplicate_identity"])
    assert a2.id_fingerprint == fresh(db, a1).id_fingerprint
    assert rec.kinds == ["verified", "under_review"]
    blob = " ".join(f"{kyc_notify.COPY[k]}" for k in rec.kinds).lower()
    assert "duplicate" not in blob and "another account" not in blob  # the caregiver is told only "under review"


def test_the_same_number_of_a_different_kind_is_not_a_duplicate(db, rec, fake_prembly):
    first, second = cg(db), cg(db)
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_prembly.script_clean_pass("sdk_session_1", first, id_type="nin")
    fake_prembly.script_clean_pass("sdk_session_2", second, id_type="bvn")
    run(db, rec, "sdk_session_1", a1)
    run(db, rec, "sdk_session_2", a2)
    assert fresh(db, second).identity_verification_status == "verified"


def test_a_failed_or_unfinished_attempt_with_the_same_number_does_not_count(db, rec, fake_prembly):
    other = cg(db)
    attempt(db, other, status="needs_review", id_fingerprint="x" * 64)
    me = cg(db)
    a = attempt(db, me)
    fake_prembly.script_clean_pass("sdk_session_a", me, id_number=SANDBOX_BVN, id_type="bvn")
    run(db, rec, "sdk_session_a", a)
    assert fresh(db, me).identity_verification_status == "verified"


def test_no_number_in_the_payload_means_no_duplicate_check_and_no_fingerprint(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, id_number=None)
    run(db, rec, "sdk_session_a", a)
    a = fresh(db, a)
    assert (a.status, a.id_fingerprint) == ("passed", None)


def test_the_number_itself_is_stored_nowhere(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    run(db, rec, "sdk_session_a", a)
    for table, column in db.execute(text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                                         "AND table_name IN ('identity_verifications', 'identity_verification_events', "
                                         "'kyc_webhook_events', 'caregivers')")).all():
        hits = db.execute(text(f'SELECT count(*) FROM "{table}" WHERE "{column}"::text LIKE :n'), {"n": f"%{SANDBOX_NIN}%"}).scalar()
        assert hits == 0, f"{table}.{column} holds the ID number"


# --- no event ever overwrites a human decision -----------------------------------------------------------------------
@pytest.mark.parametrize("human", ["verified", "failed", "needs_review"])
def test_a_provider_event_never_changes_a_caregiver_a_person_decided(db, rec, fake_prembly, human):
    caregiver = cg(db, identity_verification_status=human)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    result = run(db, rec, "sdk_session_a", a)
    assert result.outcome == "ignored_terminal" and result.notice is None
    a = fresh(db, a)
    assert fresh(db, caregiver).identity_verification_status == human
    assert (a.status, a.completed_at, a.provider_session_id) == ("awaiting_result", None, None)  # not even bound
    assert db.query(IdentityVerificationEvent).count() == 0 and rec.notices == []


def test_an_admin_decision_that_lands_while_the_worker_waits_for_the_lock_wins(db, rec, fake_prembly):
    """The admin holds the caregiver row (decide_identity locks it first); the worker queues behind and then finds a decided caregiver."""
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    box = {}
    ref = SimpleNamespace(reference=a.reference)  # read here: a session's instances are not shared with the worker thread

    def worker():
        s = SessionLocal()
        try:
            box["result"] = run(s, rec, "sdk_session_a", ref).outcome
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            s.close()

    admin = SessionLocal()
    try:
        admin.query(Caregiver).filter(Caregiver.id == caregiver.id).with_for_update().one().identity_verification_status = "failed"
        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not _waiting_on_a_lock():
            time.sleep(0.05)
        assert _waiting_on_a_lock(), "the worker should be waiting for the admin's lock"
        admin.commit()
    finally:
        admin.close()
    thread.join(timeout=15)
    assert "error" not in box, box.get("error")
    assert box["result"] == "ignored_terminal"
    assert fresh(db, caregiver).identity_verification_status == "failed" and fresh(db, a).status == "awaiting_result"
    assert rec.notices == []


def _waiting_on_a_lock() -> bool:
    s = SessionLocal()
    try:
        return bool(s.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()")).scalar())
    finally:
        s.close()


def test_an_attempt_that_is_already_decided_is_inert(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, status="failed")
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    assert run(db, rec, "sdk_session_a", a).outcome == "ignored_terminal"
    assert fresh(db, a).status == "failed" and fresh(db, caregiver).identity_verification_status == "in_progress"
    assert len(fake_prembly.requests) == 0  # not even a pull


# --- a forged event can neither verify nor taint ---------------------------------------------------------------------
def test_a_session_that_is_not_the_attempts_decides_nothing(db, rec, fake_prembly):
    victim, attacker = cg(db), cg(db)
    a = attempt(db, victim)
    fake_prembly.script_clean_pass("sdk_session_a", attacker)  # the attacker's own perfect session, named against the victim's attempt
    result = run(db, rec, "sdk_session_a", a)
    assert result.outcome == "disagreement" and result.notice is None
    assert fresh(db, victim).identity_verification_status == "in_progress" and fresh(db, a).status == "awaiting_result"
    assert fresh(db, a).provider_session_id is None and rec.notices == []


def test_a_pull_that_is_not_final_decides_nothing(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, overall_result="in_progress")
    run(db, rec, "sdk_session_a", a)
    assert (fresh(db, a).status, fresh(db, caregiver).identity_verification_status) == ("awaiting_result", "in_progress")
    assert rec.notices == []


# --- the provider flag and the wallet alert --------------------------------------------------------------------------
def test_three_outage_errors_in_a_row_flag_the_provider_unavailable(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.server_error()
    for _ in range(3):
        assert rec.availability.available
        run(db, rec, "sdk_session_a", a)
    assert rec.availability.available is False and rec.alerts == []  # an outage is not an ops alert


def test_a_good_pull_clears_the_streak_and_the_flag(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    rec.availability.errors, rec.availability.available = 2, False
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    run(db, rec, "sdk_session_a", a)
    assert (rec.availability.errors, rec.availability.available) == (0, True)


@pytest.mark.parametrize("arrange,kind", [("wallet_empty", "wallet_empty"), ("unauthorised", "unauthorised")])
def test_an_empty_wallet_or_a_refused_key_flags_at_once_and_alerts_once(db, rec, fake_prembly, arrange, kind):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    getattr(fake_prembly, arrange)()
    run(db, rec, "sdk_session_a", a)
    assert rec.availability.available is False and rec.alerts == [kind]
    run(db, rec, "sdk_session_b", a)  # a second event while it is still empty: flagged again, not alerted again
    assert rec.alerts == [kind]


def test_a_not_found_answer_is_not_an_outage(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    for _ in range(4):
        run(db, rec, "sdk_session_unknown", a)
    assert rec.availability.available is True and rec.availability.errors == 0


def test_the_real_availability_counts_to_three_with_a_ttl_and_throttles_alerts(monkeypatch):
    from app.services import kyc_availability as ka

    class Redis:
        def __init__(self):
            self.data, self.ttls = {}, {}

        def incr(self, key):
            self.data[key] = int(self.data.get(key, 0)) + 1
            return self.data[key]

        def expire(self, key, seconds):
            self.ttls[key] = seconds

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key], self.ttls[key] = value, ex
            return True

        def exists(self, key):
            return int(key in self.data)

        def delete(self, *keys):
            for k in keys:
                self.data.pop(k, None)

    fake = Redis()
    flag = ka.RedisProviderAvailability()
    monkeypatch.setattr(flag, "_redis", lambda: fake)
    assert [flag.record_error(), flag.record_error()] == [False, False] and flag.is_available()
    assert flag.record_error() is True and flag.is_available() is False and fake.ttls[ka.UNAVAILABLE_KEY] == 900
    assert fake.ttls[ka.ERRORS_KEY] == 900
    flag.record_success()
    assert flag.is_available() and ka.ERRORS_KEY not in fake.data
    assert flag.claim_alert("wallet_empty") is True and flag.claim_alert("wallet_empty") is False


def test_redis_trouble_never_blocks_a_caregiver_or_raises(monkeypatch):
    from app.services import kyc_availability as ka

    flag = ka.RedisProviderAvailability()

    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(flag, "_redis", boom)
    assert flag.is_available() is True and flag.record_error() is False and flag.claim_alert("x") is True
    flag.record_success()
    flag.mark_unavailable()


# --- what the caregiver and ops are told -----------------------------------------------------------------------------
def test_notifications_are_coarse_and_never_raise(db, monkeypatch):
    pushes, mails = [], []

    class Push:
        def send_to_users(self, db_, user_ids, *, title, body, data):
            pushes.append((list(user_ids), title, body, data))
            raise RuntimeError("expo down")

    monkeypatch.setattr(kyc_notify.push_service, "get_push_service", lambda: Push())
    monkeypatch.setattr(kyc_notify.email_service, "send_email_smtp", lambda to, subject, html: mails.append((to, subject, html)) or True)
    uid = uuid.uuid4()
    for kind in ("verified", "try_again", "under_review"):
        kyc_notify.deliver(db, kyc_notify.Notice(kind=kind, user_id=uid, email="placeholder@example.com", full_name="Test <b>Person</b>"))
    assert [p[3]["type"] for p in pushes] == ["kyc_verified", "kyc_try_again", "kyc_under_review"]
    assert len(mails) == 3 and all(m[0] == "placeholder@example.com" for m in mails)
    assert "&lt;b&gt;" in mails[0][2]  # the name is escaped by the template
    everything = " ".join(p[1] + p[2] for p in pushes).lower() + " ".join(m[2] for m in mails).lower()
    for word in ("duplicate", "watch", "mismatch", "fraud", "disagree", "score"):
        assert word not in everything


def test_the_ops_alert_logs_and_emails_when_an_address_is_set(monkeypatch, caplog):
    from app.core.config import settings

    mails = []
    monkeypatch.setattr(kyc_notify.email_service, "send_email_smtp", lambda to, subject, html: mails.append((to, html)) or True)
    kyc_notify.alert_ops("wallet_empty")
    assert mails == []  # no address configured: the ERROR log line is the alert
    monkeypatch.setattr(settings, "KYC_ALERT_EMAIL", "ops@example.com")
    with caplog.at_level("ERROR"):
        kyc_notify.alert_ops("wallet_empty")
    assert mails and mails[0][0] == "ops@example.com" and "wallet" in mails[0][1].lower()
    assert any("kyc ops alert: wallet_empty" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(kyc_notify.email_service, "send_email_smtp", lambda *a: (_ for _ in ()).throw(OSError("smtp down")))
    kyc_notify.alert_ops("unauthorised")  # never raises


def test_a_session_already_on_another_attempt_decides_nothing_and_tells_nobody(db, rec, fake_prembly):
    caregiver, other = cg(db), cg(db)
    attempt(db, other, provider_session_id="sdk_session_a")  # already on another attempt: ``_claim_session`` refuses it (email-only binding)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    result = run(db, rec, "sdk_session_a", a)
    assert result.outcome == "disagreement" and rec.notices == []
    assert fresh(db, caregiver).identity_verification_status == "in_progress"


# --- the trust rule, structurally ------------------------------------------------------------------------------------
def test_only_three_places_in_the_app_write_the_caregivers_identity_status():
    """The engine (pull-confirmed decisions), the admin decision, and K2's pending -> in_progress when an attempt starts (plus the
    model's own default). A new writer needs a deliberate edit here, and a review."""
    root = Path(__file__).resolve().parents[1]
    writers, bulk = set(), set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("tests/", "alembic/")):
            continue
        source = path.read_text()
        if re.search(r"identity_verification_status\s*=[^=]", source) or re.search(r"setattr\([^)]*identity_verification_status", source):
            writers.add(rel)
        if re.search(r"update\([^)]*identity_verification_status", source, re.S):
            bulk.add(rel)
    assert writers == {"db/models/caregiver.py", "services/caregiver_approval.py", "services/kyc_caregiver.py", "services/kyc_engine.py"}, writers
    assert not bulk, bulk


# --- a squatted session hint (K2 follow-up) ----------------------------------------------------------------------------
def test_a_squatted_hint_is_cleared_when_prembly_echoes_our_ref_for_the_real_owner(db, rec, fake_prembly):
    squatter, victim = cg(db), cg(db)
    held = attempt(db, squatter, provider_session_id="sdk_session_a")  # the client hint someone planted first
    a = attempt(db, victim)
    fake_prembly.script_clean_pass("sdk_session_a", victim, oakly_ref=str(a.reference))
    result = run(db, rec, "sdk_session_a", a)
    assert result.outcome == "applied"
    assert (fresh(db, a).status, fresh(db, a).provider_session_id) == ("passed", "sdk_session_a")
    assert fresh(db, held).provider_session_id is None and fresh(db, held).status == "awaiting_result"
    assert fresh(db, squatter).identity_verification_status == "in_progress"  # the squatter gained nothing and lost nothing else


@pytest.mark.parametrize("holder_kw", [dict(status="failed"), dict(completed_at=date(2026, 10, 1))])
def test_a_session_genuinely_held_by_a_finished_attempt_is_never_taken_over(db, rec, fake_prembly, holder_kw):
    other, mine = cg(db), cg(db)
    held = attempt(db, other, provider_session_id="sdk_session_a", **holder_kw)
    a = attempt(db, mine)
    fake_prembly.script_clean_pass("sdk_session_a", mine, oakly_ref=str(a.reference))
    assert run(db, rec, "sdk_session_a", a).outcome == "disagreement"
    assert fresh(db, held).provider_session_id == "sdk_session_a" and fresh(db, a).status == "awaiting_result"


def test_a_squatted_hint_is_not_cleared_on_an_email_only_binding(db, rec, fake_prembly):
    squatter, victim = cg(db), cg(db)
    held = attempt(db, squatter, provider_session_id="sdk_session_a")
    a = attempt(db, victim)
    fake_prembly.script_clean_pass("sdk_session_a", victim)  # no echoed ref: bound by e-mail only, which is weaker
    assert run(db, rec, "sdk_session_a", a).outcome == "disagreement"
    assert fresh(db, held).provider_session_id == "sdk_session_a"


# --- nothing the provider says about the person is kept ----------------------------------------------------------------
SENTINELS = ("SENTINELMIDDLE", "98765432101", "SENTINELPHOTO", "SENTINELADDRESS", "SENTINELKIN", "1990-04-12T00:00:00")


def test_the_full_flow_leaves_no_registry_data_in_any_table_or_log(db, rec, fake_prembly, caplog):
    caregiver = cg(db, full_name="Amina Bello")
    a = attempt(db, caregiver)
    session = fake_prembly.script_clean_pass("sdk_session_a", caregiver, id_number="98765432101",
                                             registry_name="BELLO AMINA SENTINELMIDDLE", registry_dob="1990-04-12")
    session["_payload"]["verification_response"].update({"photo": "data:image/jpeg;base64,SENTINELPHOTO", "address": "SENTINELADDRESS 1",
                                                         "next_of_kin": "SENTINELKIN"})
    session["end_user_phone"] = "SENTINELPHONE"
    with caplog.at_level(logging.DEBUG):
        run(db, rec, "sdk_session_a", a)
        from app.services.kyc_reconcile import reconcile

        reconcile(db, hooks=rec.hooks())
    assert fresh(db, a).status == "passed"
    tables = [r[0] for r in db.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")).all()]
    for table in tables:
        for column, in db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
                                       "AND table_name = :t"), {"t": table}).all():
            for sentinel in (*SENTINELS, "SENTINELPHONE"):
                hits = db.execute(text(f'SELECT count(*) FROM "{table}" WHERE "{column}"::text ILIKE :s'), {"s": f"%{sentinel}%"}).scalar()
                assert hits == 0, f"{sentinel} found in {table}.{column}"
    logged = " ".join(r.getMessage() + str(r.exc_info) + str(r.args) for r in caplog.records)
    assert not [x for x in (*SENTINELS, "SENTINELPHONE", "BELLO AMINA") if x.lower() in logged.lower()]
    assert all("SENTINEL" not in repr(n) for n in rec.notices)


# --- review fixes ----------------------------------------------------------------------------------------------------
def test_a_hint_that_lands_after_the_ownership_check_is_a_disagreement_not_a_crash(db, rec, fake_prembly, monkeypatch):
    from app.services import kyc_events

    other, mine = cg(db), cg(db)
    attempt(db, other, provider_session_id="sdk_session_a")  # appears "after" _claim_session looked
    a = attempt(db, mine)
    fake_prembly.script_clean_pass("sdk_session_a", mine, oakly_ref=str(a.reference))
    monkeypatch.setattr(kyc_events, "_claim_session", lambda *args: True)
    result = run(db, rec, "sdk_session_a", a)
    assert result.outcome == "disagreement" and fresh(db, a).status == "awaiting_result" and rec.notices == []


def test_retries_of_one_failing_pull_count_as_one_provider_error(db, rec, fake_prembly):
    from app.services.kyc_events import KycRetry

    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.server_error()
    event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id="sdk_session_a", signature_ok=True)
    db.add(event)
    db.commit()
    job = {"event_id": str(event.id), "oakly_ref": str(a.reference)}
    for _ in range(4):
        with pytest.raises(KycRetry):
            process_event(db, job, hooks=rec.hooks(), final_try=False)
    assert rec.availability.errors == 0  # ARQ's retries do not flag Prembly down on their own
    process_event(db, job, hooks=rec.hooks(), final_try=True)
    assert rec.availability.errors == 1


def test_a_closed_ledger_row_is_never_restamped(db, rec, fake_prembly):
    from app.services import kyc_events

    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    event_id = uuid.UUID(run(db, rec, "sdk_session_a", a) and db.query(KycWebhookEvent).one().id.hex)
    kyc_events._stamp(db, event_id, "ignored_terminal", None)
    db.commit()
    assert fresh(db, db.get(KycWebhookEvent, event_id)).outcome == "applied"
