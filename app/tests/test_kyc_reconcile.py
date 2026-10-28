"""K4: the reconcile cron, expiry, and the ARQ wiring.

``kyc_reconcile_job`` is the backstop for everything the webhook path can lose: unprocessed ledger rows, attempts whose webhook never
arrived, sessions Prembly never delivered, attempts nobody answered. It decides through exactly the same locked section as the
webhook worker (``kyc_events.confirm_and_record``). Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 6, 7.1, 7.3, 8.3.
All names and numbers are placeholders.
"""
import asyncio
import uuid
from datetime import date, datetime, timedelta

import pytest

from app import worker
from app.db.models.identity_verification import IdentityVerification, IdentityVerificationEvent, KycWebhookEvent
from app.services.kyc_hooks import KycHooks
from app.services.kyc_reconcile import MIN_AGE, reconcile
from app.tests.factories import make_caregiver
from app.tests.fake_prembly import FakeProviderAvailability, fake_prembly  # noqa: F401


@pytest.fixture(autouse=True)
def env(fake_prembly):  # noqa: F811
    return fake_prembly


class Recorder:
    def __init__(self):
        self.notices, self.alerts = [], []
        self.availability = FakeProviderAvailability()

    def hooks(self) -> KycHooks:
        return KycHooks(availability=self.availability, notify=lambda db, n: self.notices.append(n), alert=self.alerts.append)


@pytest.fixture
def rec():
    return Recorder()


NOW = datetime.utcnow().replace(microsecond=0)  # the cron's "now" is a parameter; real time only matters for the debounce stamp
OLD = NOW - MIN_AGE - timedelta(minutes=1)


def cg(db, **kw):
    kw.setdefault("date_of_birth", date(1990, 4, 12))
    kw.setdefault("profile_completion_step", 2)
    kw.setdefault("identity_verification_status", "in_progress")
    return make_caregiver(db, approved=False, **kw)


def attempt(db, caregiver, status="awaiting_result", *, age=None, **kw):
    """``age``: how long ago the attempt was created AND last touched (default: old enough for the cron to look)."""
    stamp = NOW - age if age is not None else OLD
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=kw.pop("attempt_no", 1), status=status,
                               created_at=kw.pop("created_at", stamp), updated_at=kw.pop("updated_at", stamp), **kw)
    db.add(row)
    db.commit()
    return row


def fresh(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


def run(db, rec, **kw):
    return reconcile(db, hooks=rec.hooks(), now=kw.pop("now", NOW), **kw)


# --- 1. webhook rows nobody processed --------------------------------------------------------------------------------
def test_a_webhook_row_nobody_processed_is_processed_by_the_cron(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    event = KycWebhookEvent(token="t1", provider_session_id="sdk_session_a", signature_ok=True, received_at=OLD)
    db.add(event)
    db.commit()
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    stats = run(db, rec)
    assert stats.reprocessed == 1
    assert fresh(db, a).status == "passed" and fresh(db, caregiver).identity_verification_status == "verified"
    assert fresh(db, event).processed_at is not None
    (trail,) = db.query(IdentityVerificationEvent).all()
    assert trail.actor == "reconcile" and [n.kind for n in rec.notices] == ["verified"]


def test_a_fresh_unprocessed_row_is_left_to_its_own_job(db, rec, fake_prembly):
    caregiver = cg(db)
    attempt(db, caregiver, provider_session_id="sdk_session_a", age=timedelta(seconds=30))
    db.add(KycWebhookEvent(token="t1", provider_session_id="sdk_session_a", signature_ok=True, received_at=NOW - timedelta(seconds=30)))
    db.commit()
    run(db, rec)
    assert fake_prembly.requests == []


# --- 2. attempts that carry a session id -----------------------------------------------------------------------------
def test_a_quiet_attempt_with_a_session_hint_is_pulled_and_decided(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    stats = run(db, rec)
    assert (stats.pulled, stats.decided) == (1, 1)
    assert fresh(db, a).status == "passed" and fresh(db, caregiver).identity_verification_status == "verified"
    assert [n.kind for n in rec.notices] == ["verified"]


def test_the_pull_decides_there_is_no_webhook_to_trust_here(db, rec, fake_prembly):
    """A clean pass reached through the cron is subject to every rule the webhook path applies (here: a name mismatch)."""
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, registry_name="CHINEDU OKAFOR")
    run(db, rec)
    assert (fresh(db, a).status, fresh(db, a).reason_codes) == ("needs_review", ["name_mismatch"])


def test_an_attempt_touched_a_moment_ago_is_left_alone(db, rec, fake_prembly):
    caregiver = cg(db)
    attempt(db, caregiver, provider_session_id="sdk_session_a", age=timedelta(minutes=1))
    run(db, rec)
    assert fake_prembly.requests == []


def test_a_session_with_no_verdict_yet_is_asked_again_only_after_the_debounce(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, overall_result="in_progress")
    run(db, rec)
    run(db, rec, now=NOW + timedelta(seconds=1))  # the first run touched it: not again straight away
    assert len(fake_prembly.requests) == 1
    assert fresh(db, a).status == "awaiting_result" and rec.notices == []
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)  # now it finished
    run(db, rec, now=datetime.utcnow() + MIN_AGE + timedelta(minutes=1))
    assert fresh(db, a).status == "passed"


def test_a_session_prembly_does_not_know_is_not_retried_every_run(db, rec, fake_prembly):
    caregiver = cg(db)
    attempt(db, caregiver, provider_session_id="sdk_session_ghost")
    run(db, rec)
    run(db, rec, now=NOW + timedelta(seconds=1))
    assert len(fake_prembly.requests) == 1 and rec.availability.errors == 0  # "not found" is an answer, not an outage


def test_a_hint_naming_someone_elses_session_decides_nothing(db, rec, fake_prembly):
    victim, other = cg(db), cg(db)
    a = attempt(db, victim, provider_session_id="sdk_session_other")
    fake_prembly.script_clean_pass("sdk_session_other", other)
    run(db, rec)
    assert fresh(db, a).status == "awaiting_result" and fresh(db, victim).identity_verification_status == "in_progress"
    assert rec.notices == []


@pytest.mark.parametrize("human", ["verified", "failed", "needs_review"])
def test_the_cron_never_overwrites_a_human_decision_and_stops_asking(db, rec, fake_prembly, human):
    caregiver = cg(db, identity_verification_status=human)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    run(db, rec)
    run(db, rec, now=NOW + timedelta(seconds=1))
    assert fresh(db, caregiver).identity_verification_status == human and fresh(db, a).status == "awaiting_result"
    assert len(fake_prembly.requests) == 1 and rec.notices == [] and db.query(IdentityVerificationEvent).count() == 0


# --- 3. attempts with no session id: the undelivered list ------------------------------------------------------------
def test_a_finished_session_the_webhook_never_delivered_is_bound_by_its_echoed_ref(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver, oakly_ref=str(a.reference), email="somebody.else@example.com")
    stats = run(db, rec)
    assert (stats.bound, stats.decided) == (1, 1)
    a = fresh(db, a)
    assert (a.status, a.provider_session_id) == ("passed", "sdk_session_a")


def test_without_an_echoed_ref_the_one_active_attempt_with_that_email_is_the_candidate(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    run(db, rec)
    assert fresh(db, a).status == "passed"


def test_two_accounts_with_one_email_are_ambiguous_so_nothing_is_bound(db, rec, fake_prembly):
    first = cg(db, email="shared@example.com")
    second = cg(db, email="shared@example.com")
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_prembly.script_clean_pass("sdk_session_a", first, email="shared@example.com")
    run(db, rec)
    assert (fresh(db, a1).status, fresh(db, a2).status) == ("awaiting_result", "awaiting_result") and rec.notices == []


def test_an_undelivered_session_nobody_owns_is_left_alone(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_x", email="stranger@example.com", face_confidence=95)
    run(db, rec)
    assert fresh(db, a).status == "awaiting_result" and rec.notices == []


def test_a_session_already_on_an_attempt_is_not_pulled_again_from_the_list(db, rec, fake_prembly):
    caregiver, other = cg(db), cg(db)
    attempt(db, other, provider_session_id="sdk_session_a", age=timedelta(minutes=1))  # known, and too fresh for step 2
    attempt(db, caregiver)
    fake_prembly.script_clean_pass("sdk_session_a", other)
    run(db, rec)
    assert [r.url.path for r in fake_prembly.requests if "undelivered" not in r.url.path] == []


def test_an_idle_system_makes_no_call_to_prembly(db, rec, fake_prembly):
    cg(db)
    run(db, rec)
    assert fake_prembly.requests == []


def test_sessions_that_are_not_finished_are_not_pulled_from_the_list(db, rec, fake_prembly):
    caregiver = cg(db)
    attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="in_progress")
    run(db, rec)
    assert all("undelivered" in r.url.path for r in fake_prembly.requests)


# --- 4. expiry -------------------------------------------------------------------------------------------------------
def test_an_attempt_nobody_answered_expires_without_touching_the_caregiver(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25))
    stats = run(db, rec)
    assert stats.expired == 1
    c, a = fresh(db, caregiver), fresh(db, a)
    assert a.status == "expired" and (c.identity_verification_status, c.profile_completion_step) == ("in_progress", 2)
    (trail,) = db.query(IdentityVerificationEvent).all()
    assert (trail.actor, trail.from_status, trail.to_status) == ("reconcile", "awaiting_result", "expired")
    assert rec.notices == []


def test_an_attempt_inside_its_window_does_not_expire(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=23))
    run(db, rec)
    assert fresh(db, a).status == "awaiting_result"


def test_an_expired_attempt_does_not_count_and_the_caregiver_can_start_again(db, rec, fake_prembly, kyc_availability):
    from app.services.kyc_caregiver import CaregiverIdentityService

    caregiver = cg(db)
    attempt(db, caregiver, age=timedelta(hours=25))
    run(db, rec)
    state = CaregiverIdentityService(db, kyc_availability).state(fresh(db, caregiver))
    assert state["attempts_used"] == 0 and state["can_start"] is True and state["active_verification_id"] is None


def test_a_result_that_arrived_late_is_decided_before_the_attempt_would_expire(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25), provider_session_id="sdk_session_a")
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    stats = run(db, rec)
    assert (fresh(db, a).status, stats.expired) == ("passed", 0)


def test_an_attempt_whose_fetch_kept_failing_is_closed_as_error_not_expired(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25), provider_session_id="sdk_session_a")
    fake_prembly.server_error()
    stats = run(db, rec)
    assert (fresh(db, a).status, stats.errored, stats.expired) == ("error", 1, 0)


def test_expiry_never_closes_a_decided_attempt_or_one_that_was_just_decided(db, rec, fake_prembly):
    caregiver = cg(db)
    done = attempt(db, caregiver, status="passed", age=timedelta(hours=48))
    run(db, rec)
    assert fresh(db, done).status == "passed"


def test_expiry_runs_even_when_the_provider_is_down_and_the_budget_is_gone(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25))
    fake_prembly.outage()
    stats = run(db, rec, budget_seconds=0)
    assert stats.out_of_time is True  # the budget stopped every provider call...
    assert fresh(db, a).status == "expired" and fake_prembly.requests == []  # ...and expiry needs none


# --- provider trouble ------------------------------------------------------------------------------------------------
def test_an_outage_stops_the_pulls_for_the_run_and_counts_once(db, rec, fake_prembly):
    for n in range(3):
        c = cg(db)
        attempt(db, c, provider_session_id=f"sdk_session_{n}")
    fake_prembly.server_error()
    stats = run(db, rec)
    assert len(fake_prembly.requests) == 1 and stats.provider_errors == 1 and rec.availability.errors == 1


def test_three_runs_in_an_outage_flag_the_provider_and_a_good_one_clears_it(db, rec, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    fake_prembly.outage()
    for minutes in (0, 10, 20):
        run(db, rec, now=NOW + timedelta(minutes=minutes))
    assert rec.availability.available is False and rec.alerts == []
    fake_prembly.recover()
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    run(db, rec, now=NOW + timedelta(minutes=30))
    assert rec.availability.available is True and fresh(db, a).status == "passed"


def test_an_empty_wallet_found_by_the_cron_flags_and_alerts_once(db, rec, fake_prembly):
    for n in range(2):
        c = cg(db)
        attempt(db, c, provider_session_id=f"sdk_session_{n}")
    fake_prembly.wallet_empty(http_402=True)
    run(db, rec)
    run(db, rec, now=NOW + timedelta(minutes=10))
    assert rec.availability.available is False and rec.alerts == ["wallet_empty"]


def test_an_empty_wallet_stops_attempts_being_started_at_once_through_the_real_api_flag(db, rec, fake_prembly, kyc_availability):
    """The flag the K2 endpoints read is the one the engine writes (here the shared fake stands in for Redis)."""
    from app.services.kyc_caregiver import CaregiverIdentityService

    caregiver = cg(db)
    attempt(db, caregiver, provider_session_id="sdk_session_a")
    fake_prembly.wallet_empty()
    reconcile(db, hooks=KycHooks(availability=kyc_availability, alert=lambda k: None), now=NOW)
    assert CaregiverIdentityService(db, kyc_availability).state(caregiver)["provider_available"] is False


# --- the ARQ wiring --------------------------------------------------------------------------------------------------
def test_the_job_and_its_cron_are_registered():
    names = {getattr(f, "name", None) or getattr(f, "__name__", None) for f in worker.WorkerSettings.functions}
    assert "kyc_reconcile_job" in names
    (cron_job,) = [c for c in worker.WorkerSettings.cron_jobs if c.coroutine is worker.kyc_reconcile_job]
    assert cron_job.minute == {3, 13, 23, 33, 43, 53} and cron_job.second == 15  # every ten minutes, off the other jobs' seconds


def test_the_job_runs_the_reconcile_with_the_production_hooks(db, rec, fake_prembly, monkeypatch):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a", age=timedelta(hours=1))
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    monkeypatch.setattr(worker.KycHooks, "production", classmethod(lambda cls: rec.hooks()))
    stats = asyncio.run(worker.kyc_reconcile_job({}))
    assert stats["decided"] == 1 and fresh(db, a).status == "passed" and [n.kind for n in rec.notices] == ["verified"]


def test_the_event_job_uses_the_production_hooks_too(db, rec, fake_prembly, monkeypatch):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id="sdk_session_a", signature_ok=True)
    db.add(event)
    db.commit()
    fake_prembly.script_clean_pass("sdk_session_a", caregiver)
    monkeypatch.setattr(worker.KycHooks, "production", classmethod(lambda cls: rec.hooks()))
    assert asyncio.run(worker.kyc_process_event({"job_try": 1}, {"event_id": str(event.id), "oakly_ref": str(a.reference)})) == "applied"
    assert [n.kind for n in rec.notices] == ["verified"]


def test_the_production_hooks_are_wired_to_real_delivery():
    from app.services import kyc_notify
    from app.services.kyc_availability import RedisProviderAvailability

    hooks = KycHooks.production()
    assert isinstance(hooks.availability, RedisProviderAvailability)
    assert hooks.notify is kyc_notify.deliver and hooks.alert is kyc_notify.alert_ops
