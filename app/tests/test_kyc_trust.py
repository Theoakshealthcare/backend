"""K4: the trust rules under pressure. Forced races (webhook job vs cron, two accounts one NIN, the advisory lock) and a randomised
property test that no sequence of events, replays, forgeries, reconciles, expiries and admin decisions can make a caregiver
``verified`` without a pull-confirmed pass, ``failed`` without an admin, or change one an admin decided.

Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 7.2, 9.1, 16.1 and 17. All names and numbers are placeholders.
"""
import random
import threading
import time
import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import IdentityVerification, IdentityVerificationEvent, KycWebhookEvent
from app.db.session import SessionLocal
from app.services.kyc_engine import ADVISORY_NAMESPACE
from app.services.kyc_events import process_event
from app.services.kyc_hooks import KycHooks
from app.services.kyc_matching import id_fingerprint
from app.services.kyc_reconcile import MIN_AGE, reconcile
from app.tests.factories import make_caregiver
from app.tests.fake_prembly import FakeProviderAvailability, SANDBOX_NIN, fake_prembly  # noqa: F401


@pytest.fixture(autouse=True)
def env(fake_prembly):  # noqa: F811
    return fake_prembly


class Recorder:
    def __init__(self):
        self.notices, self.lock = [], threading.Lock()
        self.availability = FakeProviderAvailability()

    def hooks(self) -> KycHooks:
        return KycHooks(availability=self.availability, notify=self._notify, alert=lambda kind: None)

    def _notify(self, db, notice):
        with self.lock:
            self.notices.append(notice)

    @property
    def kinds(self):
        return sorted(n.kind for n in self.notices)


@pytest.fixture
def rec():
    return Recorder()


def cg(db, **kw):
    kw.setdefault("date_of_birth", date(1990, 4, 12))
    kw.setdefault("profile_completion_step", 2)
    kw.setdefault("identity_verification_status", "in_progress")
    return make_caregiver(db, approved=False, **kw)


def attempt(db, caregiver, status="awaiting_result", **kw):
    old = datetime.utcnow() - MIN_AGE - timedelta(minutes=1)
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=kw.pop("attempt_no", 1), status=status, created_at=old,
                               updated_at=old, **kw)
    db.add(row)
    db.commit()
    return row


def fresh(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


def new_event(db, session_id):
    event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id=session_id, signature_ok=True)
    db.add(event)
    db.commit()
    return event.id


def run_event(db, rec, session_id, ref, claim=None):
    return process_event(db, {"event_id": str(new_event(db, session_id)), "oakly_ref": str(ref), "claim": claim}, hooks=rec.hooks())


def in_thread(fn, barrier=None):
    box = {}

    def go():
        s = SessionLocal()
        try:
            if barrier is not None:
                barrier.wait(timeout=10)
            box["result"] = fn(s)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            box["error"] = exc
            s.rollback()
        finally:
            s.close()

    thread = threading.Thread(target=go)
    thread.start()
    return thread, box


def waiting_on_advisory_locks() -> int:
    with SessionLocal() as probe:
        return probe.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND wait_event = 'advisory' "
                                  "AND datname = current_database()")).scalar()


# --- forced races ----------------------------------------------------------------------------------------------------
def test_the_webhook_job_and_the_cron_racing_on_one_attempt_decide_exactly_once(db, rec, fake_prembly):
    for round_no in range(4):
        caregiver = cg(db)
        a = attempt(db, caregiver, provider_session_id=f"sdk_session_{round_no}")
        fake_prembly.script_clean_pass(f"sdk_session_{round_no}", caregiver, id_number=f"{50_000_000_000 + round_no}")  # one number per round
        ref, aid = a.reference, a.id
        barrier = threading.Barrier(2)
        before = len(rec.notices)
        t1, b1 = in_thread(lambda s: run_event(s, rec, f"sdk_session_{round_no}", ref).outcome, barrier)
        t2, b2 = in_thread(lambda s: reconcile(s, hooks=rec.hooks(), now=datetime.utcnow()).decided, barrier)
        t1.join(timeout=30), t2.join(timeout=30)
        assert "error" not in b1 and "error" not in b2, (b1, b2)
        db.expire_all()
        assert db.get(IdentityVerification, aid).status == "passed"
        assert db.query(IdentityVerificationEvent).filter_by(verification_id=aid, to_status="passed").count() == 1  # decided once
        assert len(rec.notices) - before == 1  # told once
        assert db.get(Caregiver, caregiver.id).identity_verification_status == "verified"


def test_two_accounts_presenting_one_nin_at_once_cannot_both_pass(db, rec, fake_prembly):
    for round_no in range(5):
        first, second = cg(db), cg(db)
        a1, a2 = attempt(db, first), attempt(db, second)
        # a number unique to this round so earlier rounds do not interfere
        number = f"{20_000_000_000 + round_no}"
        fake_prembly.script_clean_pass(f"sdk_session_{round_no}_1", first, id_number=number)
        fake_prembly.script_clean_pass(f"sdk_session_{round_no}_2", second, id_number=number)
        barrier = threading.Barrier(2)
        refs = [(a1.reference, f"sdk_session_{round_no}_1"), (a2.reference, f"sdk_session_{round_no}_2")]
        threads = [in_thread(lambda s, r=r, sid=sid: run_event(s, rec, sid, r).outcome, barrier) for r, sid in refs]
        for t, _ in threads:
            t.join(timeout=30)
        assert all("error" not in b for _, b in threads), [b for _, b in threads]
        db.expire_all()
        statuses = sorted(db.get(IdentityVerification, a.id).status for a in (a1, a2))
        assert statuses == ["needs_review", "passed"], statuses
        loser = next(a for a in (a1, a2) if db.get(IdentityVerification, a.id).status == "needs_review")
        assert db.get(IdentityVerification, loser.id).reason_codes == ["duplicate_identity"]
        assert sorted(db.get(Caregiver, c.id).identity_verification_status for c in (first, second)) == ["needs_review", "verified"]


def test_the_advisory_lock_is_what_serialises_one_number(db, rec, fake_prembly):
    """A third session holds the number's advisory lock: both deciding workers must queue on it (proving the lock is taken), and
    once it is released exactly one of them passes."""
    first, second = cg(db), cg(db)
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_prembly.script_clean_pass("sdk_session_1", first)
    fake_prembly.script_clean_pass("sdk_session_2", second)
    fingerprint = id_fingerprint("nin", SANDBOX_NIN)
    holder = SessionLocal()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:fp))"), {"ns": ADVISORY_NAMESPACE, "fp": fingerprint})
        threads = [in_thread(lambda s, r=a.reference, sid=sid: run_event(s, rec, sid, r).outcome)
                   for a, sid in ((a1, "sdk_session_1"), (a2, "sdk_session_2"))]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and waiting_on_advisory_locks() < 2:
            time.sleep(0.05)
        assert waiting_on_advisory_locks() == 2, "both decisions should be queued on the number's advisory lock"
        holder.commit()  # releases it
    finally:
        holder.close()
    for t, _ in threads:
        t.join(timeout=30)
    assert all("error" not in b for _, b in threads), [b for _, b in threads]
    db.expire_all()
    assert sorted(db.get(IdentityVerification, a.id).status for a in (a1, a2)) == ["needs_review", "passed"]


def test_different_numbers_do_not_wait_for_each_other(db, rec, fake_prembly):
    first, second = cg(db), cg(db)
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_prembly.script_clean_pass("sdk_session_1", first, id_number="30000000001")
    fake_prembly.script_clean_pass("sdk_session_2", second, id_number="30000000002")
    holder = SessionLocal()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:fp))"), {"ns": ADVISORY_NAMESPACE, "fp": id_fingerprint("nin", "30000000001")})
        thread, box = in_thread(lambda s: run_event(s, rec, "sdk_session_2", a2.reference).outcome)
        thread.join(timeout=15)
        assert not thread.is_alive() and box["result"] == "applied"  # the other number's lock is not its lock
        holder.rollback()
    finally:
        holder.close()
    assert fresh(db, a2).status == "passed"


# --- the trust property -----------------------------------------------------------------------------------------------
MUTATIONS = ["none", "none", "none", "name", "dob", "low_face", "grey_face", "watchlisted", "not_final", "failed_verdict", "dup_number"]


def _script(fake, seq, who, caregivers, mutation, sid):
    caregiver = caregivers[who]
    number = f"{40_000_000_000 + seq * 10 + who}"
    extra = {}
    if mutation == "name":
        extra["registry_name"] = "CHINEDU OKAFOR"
    elif mutation == "dob":
        extra["registry_dob"] = "1991-04-12"
    elif mutation == "low_face":
        extra["face_confidence"] = 30.0
    elif mutation == "grey_face":
        extra["face_confidence"] = 78.0
    elif mutation == "watchlisted":
        extra["watchlisted"] = True
    elif mutation == "not_final":
        extra["overall_result"] = "in_progress"
    elif mutation == "failed_verdict":
        extra["overall_result"] = "failed"
    elif mutation == "dup_number":
        number = f"{40_000_000_000 + seq * 10 + (1 - who)}"  # the OTHER caregiver's number
    fake.script_clean_pass(sid, caregiver, id_number=number, **extra)
    return mutation in ("none", "dup_number")  # a final, fully matching pass for ``who``


def _check(db, caregivers, admin, confirmed):
    db.expire_all()
    numbers = {}
    for who, caregiver in enumerate(caregivers):
        c = db.get(Caregiver, caregiver.id)
        status = c.identity_verification_status
        events = db.query(IdentityVerificationEvent).filter_by(caregiver_id=c.id).all()
        # (1) never verified without a pull-confirmed pass (or an admin saying so)
        if status == "verified" and admin[who] != "verified":
            assert confirmed[who], "verified without any passing session ever having been scripted for this caregiver"
            assert any(e.to_status == "passed" and e.actor in ("provider", "reconcile") for e in events)
        # (2) failed only through an admin
        if status == "failed":
            assert admin[who] == "failed"
        # (3) an admin's word is final against the provider
        if admin[who] is not None:
            assert status == admin[who], f"a provider event moved a caregiver an admin set to {admin[who]}: {status}"
        # (4) a passed attempt passed every check
        for a in db.query(IdentityVerification).filter_by(caregiver_id=c.id, status="passed"):
            assert a.decided_via == "provider" and a.completed_at is not None
            assert float(a.face_confidence) >= 85 and a.name_match is True and a.dob_match is True
            numbers.setdefault(a.id_fingerprint, set()).add(c.id)
    # (5) one identity is never passed on two accounts
    assert all(len(owners) == 1 for fp, owners in numbers.items() if fp is not None)


@pytest.mark.parametrize("seed", [11, 22, 33, 44])
def test_property_no_sequence_verifies_without_a_pull_confirmed_pass_or_overwrites_an_admin(db, rec, fake_prembly, seed):
    rng = random.Random(seed)
    for seq in range(3):
        caregivers = [cg(db), cg(db)]
        attempts = [[attempt(db, c)] for c in caregivers]
        admin, confirmed = [None, None], [False, False]
        counter = 0
        for _ in range(10):
            who = rng.randrange(2)
            op = rng.choice(["event", "event", "event", "forged", "reconcile", "expire", "admin", "new_attempt", "replay"])
            if op in ("event", "forged", "replay"):
                active = [a for a in db.query(IdentityVerification).filter_by(caregiver_id=caregivers[who].id).all()
                          if a.status in ("created", "started", "awaiting_result")]
                target = active[0] if active else attempts[who][0]
                counter += 1
                sid = f"sdk_{seed}_{seq}_{counter}"
                owner = who
                if op == "forged":
                    owner = 1 - who  # somebody else's perfect session, named against this caregiver's attempt
                if op == "replay" and counter > 1:
                    sid = f"sdk_{seed}_{seq}_{rng.randrange(1, counter)}"  # an old session again
                    fake_prembly.sessions.setdefault(sid, None)
                    if fake_prembly.sessions[sid] is None:
                        del fake_prembly.sessions[sid]
                        _script(fake_prembly, seq + 100 * seed, owner, caregivers, "none", sid)
                else:
                    ok = _script(fake_prembly, seq + 100 * seed, owner, caregivers, rng.choice(MUTATIONS), sid)
                    confirmed[owner] = confirmed[owner] or ok
                claim = rng.choice([None, "pass", "pass", "fail"])
                run_event(db, rec, sid, target.reference, claim)
            elif op == "reconcile":
                reconcile(db, hooks=rec.hooks(), now=datetime.utcnow() + timedelta(minutes=10))
            elif op == "expire":
                reconcile(db, hooks=rec.hooks(), now=datetime.utcnow() + timedelta(hours=48))
            elif op == "admin":
                decision = rng.choice(["verified", "failed"])
                c = db.query(Caregiver).filter_by(id=caregivers[who].id).with_for_update().one()
                c.identity_verification_status = decision  # what CaregiverApprovalService.decide_identity does, under the same lock
                db.commit()
                admin[who] = decision
            elif op == "new_attempt":
                c = fresh(db, caregivers[who])
                rows = db.query(IdentityVerification).filter_by(caregiver_id=c.id).all()
                if c.identity_verification_status in ("pending", "in_progress") and not any(
                        a.status in ("created", "started", "awaiting_result") for a in rows):
                    attempts[who].append(attempt(db, c, attempt_no=max(a.attempt_no for a in rows) + 1))
            _check(db, caregivers, admin, confirmed)
