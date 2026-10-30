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

import httpx
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
from app.tests.fake_dojah import FakeProviderAvailability, SANDBOX_NIN, fake_dojah, new_reference  # noqa: F401


@pytest.fixture(autouse=True)
def env(fake_dojah):  # noqa: F811
    return fake_dojah


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
    kw.setdefault("provider_session_id", new_reference())  # the reference the server minted at start
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=kw.pop("attempt_no", 1), status=status, created_at=old,
                               updated_at=old, **kw)
    db.add(row)
    db.commit()
    return row


def fresh(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


def new_event(db, reference):
    event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id=reference, signature_ok=True)
    db.add(event)
    db.commit()
    return event.id


def run_event(db, rec, reference, claim=None):
    return process_event(db, {"event_id": str(new_event(db, reference)), "claim": claim, "settled": True}, hooks=rec.hooks())


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
def test_the_webhook_job_and_the_cron_racing_on_one_attempt_decide_exactly_once(db, rec, fake_dojah):
    for round_no in range(4):
        caregiver = cg(db)
        a = attempt(db, caregiver)
        ref, aid = a.provider_session_id, a.id
        fake_dojah.script_clean_pass(ref, caregiver, id_number=f"{50_000_000_000 + round_no}")  # one number per round
        barrier = threading.Barrier(2)
        before = len(rec.notices)
        t1, b1 = in_thread(lambda s: run_event(s, rec, ref).outcome, barrier)
        t2, b2 = in_thread(lambda s: reconcile(s, hooks=rec.hooks(), now=datetime.utcnow()).decided, barrier)
        t1.join(timeout=30), t2.join(timeout=30)
        assert "error" not in b1 and "error" not in b2, (b1, b2)
        db.expire_all()
        assert db.get(IdentityVerification, aid).status == "passed"
        assert db.query(IdentityVerificationEvent).filter_by(verification_id=aid, to_status="passed").count() == 1  # decided once
        assert len(rec.notices) - before == 1  # told once
        assert db.get(Caregiver, caregiver.id).identity_verification_status == "verified"


def test_two_accounts_presenting_one_nin_at_once_cannot_both_pass(db, rec, fake_dojah):
    for round_no in range(5):
        first, second = cg(db), cg(db)
        a1, a2 = attempt(db, first), attempt(db, second)
        # a number unique to this round so earlier rounds do not interfere
        number = f"{20_000_000_000 + round_no}"
        fake_dojah.script_clean_pass(a1.provider_session_id, first, id_number=number)
        fake_dojah.script_clean_pass(a2.provider_session_id, second, id_number=number)
        barrier = threading.Barrier(2)
        threads = [in_thread(lambda s, r=r: run_event(s, rec, r).outcome, barrier) for r in (a1.provider_session_id, a2.provider_session_id)]
        for t, _ in threads:
            t.join(timeout=30)
        assert all("error" not in b for _, b in threads), [b for _, b in threads]
        db.expire_all()
        statuses = sorted(db.get(IdentityVerification, a.id).status for a in (a1, a2))
        assert statuses == ["needs_review", "passed"], statuses
        loser = next(a for a in (a1, a2) if db.get(IdentityVerification, a.id).status == "needs_review")
        assert db.get(IdentityVerification, loser.id).reason_codes == ["duplicate_identity"]
        assert sorted(db.get(Caregiver, c.id).identity_verification_status for c in (first, second)) == ["needs_review", "verified"]


def test_the_advisory_lock_is_what_serialises_one_number(db, rec, fake_dojah):
    """A third session holds the number's advisory lock: both deciding workers must queue on it (proving the lock is taken), and
    once it is released exactly one of them passes."""
    first, second = cg(db), cg(db)
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_dojah.script_clean_pass(a1.provider_session_id, first)
    fake_dojah.script_clean_pass(a2.provider_session_id, second)
    fingerprint = id_fingerprint("nin", SANDBOX_NIN)
    holder = SessionLocal()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:fp))"), {"ns": ADVISORY_NAMESPACE, "fp": fingerprint})
        threads = [in_thread(lambda s, r=a.provider_session_id: run_event(s, rec, r).outcome) for a in (a1, a2)]
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


def test_different_numbers_do_not_wait_for_each_other(db, rec, fake_dojah):
    first, second = cg(db), cg(db)
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_dojah.script_clean_pass(a1.provider_session_id, first, id_number="30000000001")
    fake_dojah.script_clean_pass(a2.provider_session_id, second, id_number="30000000002")
    holder = SessionLocal()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:fp))"), {"ns": ADVISORY_NAMESPACE, "fp": id_fingerprint("nin", "30000000001")})
        thread, box = in_thread(lambda s: run_event(s, rec, a2.provider_session_id).outcome)
        thread.join(timeout=15)
        assert not thread.is_alive() and box["result"] == "applied"  # the other number's lock is not its lock
        holder.rollback()
    finally:
        holder.close()
    assert fresh(db, a2).status == "passed"


# --- the trust property -----------------------------------------------------------------------------------------------
MUTATIONS = ["none", "none", "none", "name", "dob", "low_face", "grey_face", "liveness_failed", "not_final", "failed_verdict",
             "dup_number", "no_government"]


def _script(fake, seq, who, caregivers, mutation, reference):
    caregiver = caregivers[who]
    number = f"{40_000_000_000 + seq * 10 + who}"
    extra = {}
    if mutation == "name":
        extra.update(first_name="CHINEDU", middle_name=None, last_name="OKAFOR")
    elif mutation == "dob":
        extra["date_of_birth"] = "12-Apr-1991"
    elif mutation == "low_face":
        extra["confidence"] = 30.0
    elif mutation == "grey_face":
        extra["confidence"] = 78.0
    elif mutation == "liveness_failed":
        extra["selfie"] = False
    elif mutation == "not_final":
        extra["status"] = "Ongoing"
    elif mutation == "failed_verdict":
        extra["status"] = "Failed"
    elif mutation == "no_government":
        extra["government"] = False
    elif mutation == "dup_number":
        number = f"{40_000_000_000 + seq * 10 + (1 - who)}"  # the OTHER caregiver's number
    fake.script_clean_pass(reference, caregiver, id_number=number, **extra)
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
            assert confirmed[who], "verified without any passing verification ever having been scripted for this caregiver"
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
def test_property_no_sequence_verifies_without_a_pull_confirmed_pass_or_overwrites_an_admin(db, rec, fake_dojah, seed):
    rng = random.Random(seed)
    for seq in range(3):
        caregivers = [cg(db), cg(db)]
        attempts = [[attempt(db, c)] for c in caregivers]
        admin, confirmed = [None, None], [False, False]
        for _ in range(10):
            who = rng.randrange(2)
            op = rng.choice(["event", "event", "event", "forged", "reconcile", "expire", "admin", "new_attempt", "replay"])
            if op in ("event", "forged", "replay"):
                active = [a for a in db.query(IdentityVerification).filter_by(caregiver_id=caregivers[who].id).all()
                          if a.status in ("created", "started", "awaiting_result")]
                target = active[0] if active else attempts[who][0]
                reference = target.provider_session_id
                if op == "forged":
                    # a signed event naming this caregiver's reference while the answer carries somebody else's perfect run
                    stranger = new_reference()
                    _script(fake_dojah, seq + 100 * seed, 1 - who, caregivers, "none", stranger)
                    fake_dojah.script(httpx.Response(200, json={"entity": fake_dojah.verifications[stranger]}))
                elif op == "event":
                    ok = _script(fake_dojah, seq + 100 * seed, who, caregivers, rng.choice(MUTATIONS), reference)
                    confirmed[who] = confirmed[who] or ok
                # "replay": the event arrives again for whatever Dojah holds under the reference now (or nothing)
                run_event(db, rec, reference, rng.choice([None, "fail"]))
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
