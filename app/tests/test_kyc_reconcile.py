"""K4: the reconcile cron, expiry, and the ARQ wiring.

``kyc_reconcile_job`` is the backstop for everything the webhook path can lose: unprocessed ledger rows, attempts whose webhook never
arrived (every attempt carries the reference the server minted, so each is pulled by it), attempts nobody answered. It decides through exactly the same locked section as the
webhook worker (``kyc_events.confirm_and_record``). Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 6, 7.1, 7.3, 8.3.
All names and numbers are placeholders.
"""
import asyncio
import uuid
from datetime import date, datetime, timedelta

import httpx
import pytest

from app import worker
from app.db.models.identity_verification import IdentityVerification, IdentityVerificationEvent, KycWebhookEvent
from app.services.kyc_hooks import KycHooks
from app.services.kyc_reconcile import MIN_AGE, reconcile
from app.tests.factories import make_caregiver
from app.tests.fake_dojah import FakeProviderAvailability, fake_dojah, new_reference  # noqa: F401


@pytest.fixture(autouse=True)
def env(fake_dojah):  # noqa: F811
    return fake_dojah


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
    kw.setdefault("provider_session_id", new_reference())  # the reference the server minted at start
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
def test_a_webhook_row_nobody_processed_is_processed_by_the_cron(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    event = KycWebhookEvent(token="t1", provider_session_id=a.provider_session_id, signature_ok=True, received_at=OLD)
    db.add(event)
    db.commit()
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    stats = run(db, rec)
    assert stats.reprocessed == 1
    assert fresh(db, a).status == "passed" and fresh(db, caregiver).identity_verification_status == "verified"
    assert fresh(db, event).processed_at is not None
    (trail,) = db.query(IdentityVerificationEvent).all()
    assert trail.actor == "reconcile" and [n.kind for n in rec.notices] == ["verified"]


def test_a_fresh_unprocessed_row_is_left_to_its_own_job(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(seconds=30))
    db.add(KycWebhookEvent(token="t1", provider_session_id=a.provider_session_id, signature_ok=True, received_at=NOW - timedelta(seconds=30)))
    db.commit()
    run(db, rec)
    assert fake_dojah.requests == []


# --- 2. quiet attempts: pulled by their own reference -----------------------------------------------------------------------------
def test_a_quiet_attempt_is_pulled_by_its_own_reference_and_decided(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    stats = run(db, rec)
    assert (stats.pulled, stats.decided) == (1, 1) and fake_dojah.references_pulled == [a.provider_session_id]
    assert fresh(db, a).status == "passed" and fresh(db, caregiver).identity_verification_status == "verified"
    assert [n.kind for n in rec.notices] == ["verified"]


def test_the_pull_decides_there_is_no_webhook_to_trust_here(db, rec, fake_dojah):
    """A clean pass reached through the cron is subject to every rule the webhook path applies (here: a name mismatch)."""
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver, first_name="CHINEDU", middle_name=None, last_name="OKAFOR")
    run(db, rec)
    assert (fresh(db, a).status, fresh(db, a).reason_codes) == ("needs_review", ["name_mismatch"])


def test_an_attempt_touched_a_moment_ago_is_left_alone(db, rec, fake_dojah):
    caregiver = cg(db)
    attempt(db, caregiver, age=timedelta(minutes=1))
    run(db, rec)
    assert fake_dojah.requests == []


@pytest.mark.parametrize("status", ["Ongoing", "Pending", "Abandoned"])
def test_a_verification_with_no_verdict_yet_is_asked_again_only_after_the_debounce(db, rec, fake_dojah, status):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver, status=status)
    run(db, rec)
    run(db, rec, now=NOW + timedelta(seconds=1))  # the first run touched it: not again straight away
    assert len(fake_dojah.requests) == 1
    assert fresh(db, a).status == "awaiting_result" and rec.notices == []
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)  # now it finished
    run(db, rec, now=datetime.utcnow() + MIN_AGE + timedelta(minutes=1))
    assert fresh(db, a).status == "passed"


def test_a_reference_dojah_does_not_know_is_not_retried_every_run(db, rec, fake_dojah):
    """Normal for a widget nobody opened: the reference was minted before the launch. An answer, not an outage and not an error."""
    caregiver = cg(db)
    attempt(db, caregiver)  # nothing scripted: the pull answers 404
    stats = run(db, rec)
    run(db, rec, now=NOW + timedelta(seconds=1))
    assert len(fake_dojah.requests) == 1 and rec.availability.errors == 0 and stats.provider_errors == 0


def test_an_answer_that_names_another_reference_decides_nothing(db, rec, fake_dojah):
    victim, other = cg(db), cg(db)
    a = attempt(db, victim)
    stranger = new_reference()
    fake_dojah.script_clean_pass(stranger, other)
    fake_dojah.script(httpx.Response(200, json={"entity": fake_dojah.verifications[stranger]}))
    run(db, rec)
    assert fresh(db, a).status == "awaiting_result" and fresh(db, victim).identity_verification_status == "in_progress"
    assert rec.notices == []


@pytest.mark.parametrize("human", ["verified", "failed", "needs_review"])
def test_the_cron_never_overwrites_a_human_decision_and_stops_asking(db, rec, fake_dojah, human):
    caregiver = cg(db, identity_verification_status=human)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    run(db, rec)
    run(db, rec, now=NOW + timedelta(seconds=1))
    assert fresh(db, caregiver).identity_verification_status == human and fresh(db, a).status == "awaiting_result"
    assert len(fake_dojah.requests) == 1 and rec.notices == [] and db.query(IdentityVerificationEvent).count() == 0


# --- 3. nothing to discover ---------------------------------------------------------------------------------------------
def test_every_quiet_attempt_is_pulled_by_its_own_reference_and_no_list_is_ever_asked_for(db, rec, fake_dojah):
    first, second = cg(db), cg(db)
    a1, a2 = attempt(db, first), attempt(db, second)
    fake_dojah.script_clean_pass(a1.provider_session_id, first)
    fake_dojah.script_clean_pass(a2.provider_session_id, second, id_number="70000000099")
    stats = run(db, rec)
    assert stats.decided == 2 and sorted(fake_dojah.references_pulled) == sorted([a1.provider_session_id, a2.provider_session_id])
    assert {r.url.path for r in fake_dojah.requests} == {"/api/v1/kyc/verification"}


def test_a_finished_run_belongs_to_the_attempt_that_minted_its_reference_whatever_the_email(db, rec, fake_dojah):
    """The old e-mail and echoed-ref bindings are gone: only the reference the server minted for an attempt reaches that attempt."""
    mine, theirs = cg(db), cg(db)
    a1, a2 = attempt(db, mine), attempt(db, theirs)
    fake_dojah.script_clean_pass(a2.provider_session_id, theirs)
    run(db, rec)
    assert fresh(db, a1).status == "awaiting_result" and fresh(db, mine).identity_verification_status == "in_progress"
    assert fresh(db, a2).status == "passed"


def test_an_idle_system_makes_no_call_to_dojah(db, rec, fake_dojah):
    run(db, rec)
    assert fake_dojah.requests == []


def test_a_terminal_attempt_is_never_pulled(db, rec, fake_dojah):
    caregiver = cg(db)
    attempt(db, caregiver, status="passed")
    run(db, rec)
    assert fake_dojah.requests == []


# --- 4. expiry (step 3 of the job) -------------------------------------------------------------------------------------------------------
def test_an_attempt_nobody_answered_expires_without_touching_the_caregiver(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25))  # Dojah never heard of the reference (404)
    stats = run(db, rec)
    assert stats.expired == 1
    c, a = fresh(db, caregiver), fresh(db, a)
    assert a.status == "expired" and (c.identity_verification_status, c.profile_completion_step) == ("in_progress", 2)
    (trail,) = db.query(IdentityVerificationEvent).all()
    assert (trail.actor, trail.from_status, trail.to_status) == ("reconcile", "awaiting_result", "expired")
    assert rec.notices == []


def test_an_attempt_inside_its_window_does_not_expire(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=23))
    run(db, rec)
    assert fresh(db, a).status == "awaiting_result"


def test_an_expired_attempt_does_not_count_and_the_caregiver_can_start_again(db, rec, fake_dojah, kyc_availability):
    from app.services.kyc_caregiver import CaregiverIdentityService

    caregiver = cg(db)
    attempt(db, caregiver, age=timedelta(hours=25))
    run(db, rec)
    state = CaregiverIdentityService(db, kyc_availability).state(fresh(db, caregiver))
    assert state["attempts_used"] == 0 and state["can_start"] is True and state["active_verification_id"] is None


def test_a_result_that_arrived_late_is_decided_before_the_attempt_would_expire(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25))
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    stats = run(db, rec)
    assert (fresh(db, a).status, stats.expired) == ("passed", 0)


def test_an_attempt_dojah_refuses_to_answer_for_is_closed_as_error_not_expired(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25))
    fake_dojah.script(httpx.Response(422, json={"error": "bad reference"}))
    stats = run(db, rec)
    assert (fresh(db, a).status, stats.errored, stats.expired) == ("error", 1, 0)


def test_expiry_never_closes_a_decided_attempt_or_one_that_was_just_decided(db, rec, fake_dojah):
    caregiver = cg(db)
    done = attempt(db, caregiver, status="passed", age=timedelta(hours=48))
    run(db, rec)
    assert fresh(db, done).status == "passed"


def test_expiry_never_throws_away_what_it_could_not_check(db, rec, fake_dojah):
    """A billed result may be waiting at Dojah: an attempt is expired only when this run KNOWS there is none."""
    caregiver = cg(db)
    hinted = attempt(db, caregiver, age=timedelta(hours=25))
    fake_dojah.outage()
    stats = run(db, rec)  # the provider is down: attempts stay as they are (design 7.3)
    assert fresh(db, hinted).status == "awaiting_result" and stats.expired == 0
    fake_dojah.recover()
    fake_dojah.script_clean_pass(hinted.provider_session_id, caregiver, status="Ongoing")
    db.query(IdentityVerification).filter_by(id=hinted.id).update({"updated_at": NOW})  # inside the debounce: not pulled this run
    db.commit()
    run(db, rec)
    assert fresh(db, hinted).status == "awaiting_result"


@pytest.mark.parametrize("status", ["Ongoing", "Pending", "Abandoned"])
def test_an_attempt_is_expired_once_dojah_answered_with_no_verdict(db, rec, fake_dojah, status):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25))
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver, status=status)
    run(db, rec)
    assert fresh(db, a).status == "expired"


def test_an_attempt_is_not_expired_when_the_run_ran_out_of_time_before_asking(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=25))
    stats = run(db, rec, budget_seconds=0)
    assert stats.out_of_time is True and fresh(db, a).status == "awaiting_result" and fake_dojah.requests == []


def test_one_rows_trouble_does_not_stop_the_run(db, rec, fake_dojah, monkeypatch):
    from app.services import kyc_reconcile

    stale = attempt(db, cg(db), age=timedelta(hours=25))
    boom = cg(db)
    b = attempt(db, boom)
    fake_dojah.script_clean_pass(b.provider_session_id, boom)
    monkeypatch.setattr(kyc_reconcile, "settle_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    stats = run(db, rec)
    assert fresh(db, stale).status == "expired" and stats.expired == 1  # the later steps still ran


# --- provider trouble ------------------------------------------------------------------------------------------------
def test_an_outage_stops_the_pulls_for_the_run_and_counts_once(db, rec, fake_dojah):
    for n in range(3):
        c = cg(db)
        attempt(db, c)
    fake_dojah.server_error()
    stats = run(db, rec)
    assert len(set(fake_dojah.references_pulled)) == 1 and len(fake_dojah.requests) == 3  # one pull = the client's 3 attempts
    assert stats.provider_errors == 1 and rec.availability.errors == 1


def test_three_runs_in_an_outage_flag_the_provider_and_a_good_one_clears_it(db, rec, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.outage()
    for minutes in (0, 10, 20):
        run(db, rec, now=NOW + timedelta(minutes=minutes))
    assert rec.availability.available is False and rec.alerts == []
    fake_dojah.recover()
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    run(db, rec, now=NOW + timedelta(minutes=30))
    assert rec.availability.available is True and fresh(db, a).status == "passed"


def test_an_empty_wallet_found_by_the_cron_flags_and_alerts_once(db, rec, fake_dojah):
    for n in range(2):
        c = cg(db)
        attempt(db, c)
    fake_dojah.wallet_empty()
    run(db, rec)
    run(db, rec, now=NOW + timedelta(minutes=10))
    assert rec.availability.available is False and rec.alerts == ["wallet_empty"]


def test_an_empty_wallet_stops_attempts_being_started_at_once_through_the_real_api_flag(db, rec, fake_dojah, kyc_availability):
    """The flag the K2 endpoints read is the one the engine writes (here the shared fake stands in for Redis)."""
    from app.services.kyc_caregiver import CaregiverIdentityService

    caregiver = cg(db)
    attempt(db, caregiver)
    fake_dojah.wallet_empty()
    reconcile(db, hooks=KycHooks(availability=kyc_availability, alert=lambda k: None), now=NOW)
    assert CaregiverIdentityService(db, kyc_availability).state(caregiver)["provider_available"] is False


# --- the ARQ wiring --------------------------------------------------------------------------------------------------
def test_the_job_and_its_cron_are_registered():
    names = {getattr(f, "name", None) or getattr(f, "__name__", None) for f in worker.WorkerSettings.functions}
    assert "kyc_reconcile_job" in names
    (cron_job,) = [c for c in worker.WorkerSettings.cron_jobs if c.coroutine is worker.kyc_reconcile_job]
    assert cron_job.minute == {3, 13, 23, 33, 43, 53} and cron_job.second == 15  # every ten minutes, off the other jobs' seconds


def test_the_job_runs_the_reconcile_with_the_production_hooks(db, rec, fake_dojah, monkeypatch):
    caregiver = cg(db)
    a = attempt(db, caregiver, age=timedelta(hours=1))
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    monkeypatch.setattr(worker.KycHooks, "production", classmethod(lambda cls: rec.hooks()))
    stats = asyncio.run(worker.kyc_reconcile_job({}))
    assert stats["decided"] == 1 and fresh(db, a).status == "passed" and [n.kind for n in rec.notices] == ["verified"]


def test_the_event_job_uses_the_production_hooks_too(db, rec, fake_dojah, monkeypatch):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    event = KycWebhookEvent(token=uuid.uuid4().hex, provider_session_id=a.provider_session_id, signature_ok=True)
    db.add(event)
    db.commit()
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    monkeypatch.setattr(worker.KycHooks, "production", classmethod(lambda cls: rec.hooks()))
    assert asyncio.run(worker.kyc_process_event({"job_try": 1}, {"event_id": str(event.id), "claim": None, "settled": True})) == "applied"
    assert [n.kind for n in rec.notices] == ["verified"]


def test_the_production_hooks_are_wired_to_real_delivery():
    from app.services import kyc_notify
    from app.services.kyc_availability import RedisProviderAvailability

    hooks = KycHooks.production()
    assert isinstance(hooks.availability, RedisProviderAvailability)
    assert hooks.notify is kyc_notify.deliver and hooks.alert is kyc_notify.alert_ops


def test_one_attempt_whose_answer_names_another_reference_cannot_stall_the_run(db, rec, fake_dojah):
    """It is not an outage: the other attempts are still pulled, nothing is flagged down, stale attempts still expire, and the odd
    one is debounced instead of being first in line on every run."""
    odd = attempt(db, cg(db), updated_at=OLD - timedelta(minutes=30))  # the oldest: first in line
    good_caregiver = cg(db)
    good = attempt(db, good_caregiver)
    stale = attempt(db, cg(db), age=timedelta(hours=25))
    fake_dojah.script_clean_pass(good.provider_session_id, good_caregiver)
    fake_dojah.script(httpx.Response(200, json={"entity": {"reference_id": "OKL-" + "9" * 32, "verification_status": "Completed"}}))
    stats = run(db, rec)
    assert fresh(db, good).status == "passed" and fresh(db, stale).status in ("expired", "error")
    assert rec.availability.errors == 0 and rec.availability.available
    assert fresh(db, odd).updated_at > OLD  # debounced
    assert stats.provider_errors == 1
