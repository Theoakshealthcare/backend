"""K3: the worker side of the Prembly webhook. Locate the attempt, PULL the session, record the raw outcome.

K3 changes NO caregiver status and no attempt status (K4 does that), so most tests assert what stays the same. Design:
docs/plans/2026-10-27-caregiver-kyc-design.md sections 8.3, 8.4 and 16.1. Names, ids and numbers are placeholders.
"""
import asyncio
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime

import httpx
import pytest
from arq import Retry
from sqlalchemy import text

from app import worker
from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, VERIFICATION_STATUSES, VERIFICATION_TERMINAL_STATUSES, IdentityVerification,
    IdentityVerificationEvent, KycWebhookEvent,
)
from app.db.session import SessionLocal
from app.services import kyc_events
from app.services.kyc_events import KycRetry, lock_caregiver_then_attempt, process_event
from app.tests.factories import make_caregiver
from app.tests.fake_prembly import (  # noqa: F401
    SANDBOX_NIN, TEST_WEBHOOK_KEY, fake_prembly, kyc_queue, signed_webhook, webhook_payload,
)

URL = "/api/v1/kyc/prembly/webhook"
SENTINEL_PHOTO = "data:image/jpeg;base64,SENTINEL-PHOTO-do-not-store"


@pytest.fixture(autouse=True)
def env(fake_prembly, kyc_queue):  # noqa: F811
    return fake_prembly


def cg(db, **kw):
    return make_caregiver(db, approved=False, profile_completion_step=2, **kw)


def attempt(db, caregiver, status="awaiting_result", **kw):
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=kw.pop("attempt_no", 1), status=status, **kw)
    db.add(row)
    db.commit()
    return row


def deliver(client, kyc_queue, session_id, *, ref=None, status="VERIFIED", token=None, **payload_kw):
    """POST a signed webhook through the real endpoint; returns the job the endpoint queued."""
    before = len(kyc_queue.jobs)
    payload = webhook_payload(session_id, status=status, oakly_ref=str(ref) if ref else None, **payload_kw)
    body, headers = signed_webhook(payload, TEST_WEBHOOK_KEY, token=token)
    assert client.post(URL, content=body, headers=headers).status_code == 200
    assert len(kyc_queue.jobs) == before + 1
    return kyc_queue.jobs[-1]


def ledger(db, job):
    db.expire_all()
    return db.get(KycWebhookEvent, uuid.UUID(job["event_id"]))


def fresh(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


def snapshot(a):
    return (a.status, a.provider_session_id, a.reason_codes, a.face_confidence, a.liveness_confidence, a.provider_reference,
            a.provider_cost, a.completed_at, a.decided_via)


# --- a pull-confirmed result is recorded, nothing is decided ---------------------------------------------------------
def test_a_confirmed_result_is_recorded_from_the_pull_and_decides_nothing(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, face_confidence=92.5, liveness_confidence=0.9,
                                total_cost="310.00",
                                payload={"face_data": {"confidence": 92.5}, "biometric_results": {"liveness_confidence": 0.9},
                                         "verification": {"reference": "prem-ref-1"}})
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)

    result = process_event(db, job)

    assert result.outcome == "applied" and result.verification_id == a.id and result.observation.verdict == "pass"
    a = fresh(db, a)
    assert (a.provider_session_id, a.provider_reference) == ("sdk_session_a", "prem-ref-1")
    assert (float(a.face_confidence), float(a.liveness_confidence), float(a.provider_cost)) == (92.5, 90.0, 310.0)
    assert a.completed_at is not None and a.reason_codes == []
    # K4's job, not K3's: no attempt status, no decision, no caregiver status, no step change, no system-decision trail
    assert a.status == "awaiting_result" and a.decided_via is None and a.face_threshold is None
    c = fresh(db, caregiver)
    assert (c.identity_verification_status, c.profile_completion_step, c.profile_status) == ("pending", 2, "under_review")
    assert db.query(IdentityVerificationEvent).count() == 0
    event = ledger(db, job)
    assert (event.outcome, event.verification_id) == ("applied", a.id) and event.processed_at is not None


def test_the_pull_uses_the_secret_key_and_asks_for_the_payload(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email)
    process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference))
    (request,) = fake_prembly.requests
    assert request.url.path.endswith("/sdk_session_a/") and request.url.params["include_payload"] == "true"
    assert request.headers["x-api-key"] and request.headers["x-organisation-id"]


@pytest.mark.parametrize("status", VERIFICATION_ACTIVE_STATUSES)
def test_no_active_status_is_changed(client, db, kyc_queue, fake_prembly, status):
    caregiver = cg(db)
    a = attempt(db, caregiver, status=status)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, face_confidence=95)
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)).outcome == "applied"
    assert fresh(db, a).status == status


def test_a_session_that_is_not_final_is_only_bound(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="in_progress", face_confidence=95)
    result = process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference, status="IN_PROGRESS"))
    a = fresh(db, a)
    assert result.outcome == "applied" and result.observation.final is False
    assert a.provider_session_id == "sdk_session_a" and a.face_confidence is None and a.completed_at is None


def test_a_claimed_verdict_the_pull_does_not_show_yet_is_retried_not_consumed(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="in_progress")
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference, status="VERIFIED")  # the webhook claims a pass
    with pytest.raises(KycRetry):
        process_event(db, job, final_try=False)
    assert ledger(db, job).processed_at is None and fresh(db, a).provider_session_id is None  # nothing was kept

    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="passed")  # the session caught up
    assert process_event(db, job, final_try=False).outcome == "applied"
    assert fresh(db, a).completed_at is not None


def test_on_the_last_try_a_still_unfinished_session_is_recorded_as_bound_only(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="in_progress")
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference, status="VERIFIED")
    assert process_event(db, job, final_try=True).outcome == "applied"
    assert fresh(db, a).completed_at is None


def test_scores_outside_their_scale_are_dropped_not_clamped(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, face_confidence=140, liveness_confidence=90)
    process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference))
    a = fresh(db, a)
    assert a.face_confidence is None and a.liveness_confidence is None and a.completed_at is not None


# --- trust: the webhook alone is never believed -----------------------------------------------------------------------
def test_a_webhook_pass_over_a_pulled_failure_is_a_disagreement_and_records_no_result(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="failed", face_confidence=10)
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference, status="VERIFIED")
    assert process_event(db, job).outcome == "disagreement"
    a = fresh(db, a)
    assert a.reason_codes == ["provider_disagreement"] and a.provider_session_id is None and a.face_confidence is None
    assert a.status == "awaiting_result" and ledger(db, job).outcome == "disagreement"


def test_a_webhook_failure_over_a_pulled_pass_is_a_disagreement_too(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="passed")
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference, status="FAILED")).outcome == "disagreement"


def test_a_forged_webhook_cannot_attach_someone_elses_session(client, db, kyc_queue, fake_prembly):
    """The signing key may be public (U1): someone who can sign can name any ref/session. The pull says whose session it is."""
    victim = cg(db)
    a = attempt(db, victim)
    fake_prembly.script_session("sdk_session_friend", email="someone.else@example.com", face_confidence=99)
    result = process_event(db, deliver(client, kyc_queue, "sdk_session_friend", ref=a.reference))
    a = fresh(db, a)
    assert result.outcome == "disagreement" and a.provider_session_id is None and a.face_confidence is None
    assert a.reason_codes == []  # the victim's attempt is not even tainted: only the ledger row records the disagreement
    assert fresh(db, victim).identity_verification_status == "pending"


def test_an_echoed_ref_in_the_pull_is_the_binding_when_present(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email="other@example.com", payload={"metadata": {"oakly_ref": str(a.reference)}})
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)).outcome == "applied"


def test_an_echoed_ref_that_is_someone_elses_is_a_disagreement_even_if_the_email_matches(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, payload={"metadata": {"oakly_ref": str(uuid.uuid4())}})
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)).outcome == "disagreement"


def test_the_clients_session_hint_alone_needs_the_providers_own_email(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")  # the K2 client-event hint
    fake_prembly.script_session("sdk_session_a", email=caregiver.email.upper())
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_a")).outcome == "applied"  # no oakly_ref in the body
    assert fresh(db, a).completed_at is not None

    other = cg(db)
    b = attempt(db, other, provider_session_id="sdk_session_b")
    fake_prembly.script_session("sdk_session_b", email="not.the.caregiver@example.com")
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_b")).outcome == "disagreement"
    assert fresh(db, b).completed_at is None


def test_a_newer_session_bound_by_the_pull_replaces_the_clients_older_hint(client, db, kyc_queue, fake_prembly):
    """The caregiver reopened the widget: the app's stored hint is the old session, the webhook is for the new one."""
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_hint")
    fake_prembly.script_session("sdk_session_other", email=caregiver.email)
    result = process_event(db, deliver(client, kyc_queue, "sdk_session_other", ref=a.reference))
    assert result.outcome == "applied" and fresh(db, a).provider_session_id == "sdk_session_other"


def test_a_conflicting_session_that_does_not_belong_to_the_attempt_changes_nothing(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_hint")
    fake_prembly.script_session("sdk_session_other", email="someone.else@example.com")
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_other", ref=a.reference)).outcome == "disagreement"
    after = fresh(db, a)
    assert after.provider_session_id == "sdk_session_hint" and after.reason_codes == []


def test_a_session_already_bound_to_another_attempt_is_never_bound_twice(client, db, kyc_queue, fake_prembly):
    first, second = cg(db), cg(db)
    attempt(db, first, status="passed", provider_session_id="sdk_session_a")
    b = attempt(db, second)
    fake_prembly.script_session("sdk_session_a", email=second.email)
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=b.reference)).outcome == "disagreement"
    assert fresh(db, b).provider_session_id is None


def test_a_signed_webhook_for_a_session_prembly_does_not_know_is_a_disagreement(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)  # nothing scripted: the pull answers "01 record not found"
    job = deliver(client, kyc_queue, "sdk_session_ghost", ref=a.reference)
    assert process_event(db, job).outcome == "disagreement"
    # ledger only: a forged event naming a victim's ref and a made-up session must not taint the victim's attempt
    assert fresh(db, a).reason_codes == [] and ledger(db, job).processed_at is not None


# --- orphans, terminal attempts, replays ------------------------------------------------------------------------------
def test_an_orphan_event_is_recorded_without_a_pull(client, db, kyc_queue, fake_prembly):
    job = deliver(client, kyc_queue, "sdk_session_nobody", ref=uuid.uuid4())
    result = process_event(db, job)
    assert result.outcome == "orphan" and result.verification_id is None and fake_prembly.requests == []
    assert (ledger(db, job).outcome, ledger(db, job).verification_id) == ("orphan", None)


@pytest.mark.parametrize("status", VERIFICATION_TERMINAL_STATUSES)
def test_a_terminal_attempt_ignores_events_and_costs_no_pull(client, db, kyc_queue, fake_prembly, status):
    caregiver = cg(db)
    a = attempt(db, caregiver, status=status, decided_via="admin" if status == "passed" else None)
    before = snapshot(a)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, overall_result="failed")
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference, status="FAILED")
    assert process_event(db, job).outcome == "ignored_terminal"
    assert fake_prembly.requests == [] and snapshot(fresh(db, a)) == before
    assert ledger(db, job).outcome == "ignored_terminal"


def test_a_replay_under_a_new_token_changes_nothing_the_second_time(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, face_confidence=91)
    first = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    assert process_event(db, first).outcome == "applied"
    recorded = snapshot(fresh(db, a))
    replay = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    assert replay["event_id"] != first["event_id"]
    assert process_event(db, replay).outcome == "applied"
    assert snapshot(fresh(db, a)) == recorded
    assert len(fake_prembly.requests) == 1  # a result is already recorded: the replay cost no pull
    assert db.query(IdentityVerificationEvent).count() == 0


def test_a_job_delivered_twice_is_processed_once(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email)
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    assert process_event(db, job).outcome == "applied"
    assert process_event(db, job).outcome == "applied"
    assert len(fake_prembly.requests) == 1


def test_a_missing_ledger_row_is_not_an_error(db):
    assert process_event(db, {"event_id": str(uuid.uuid4())}).outcome == "error"


def test_a_ledger_row_whose_session_id_was_erased_is_an_orphan(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    job = deliver(client, kyc_queue, "sdk_session_a")
    db.query(KycWebhookEvent).update({"provider_session_id": None})
    db.commit()
    assert process_event(db, {**job, "oakly_ref": None}).outcome == "orphan" and fake_prembly.requests == []
    assert fresh(db, a).provider_session_id is None


# --- the pull fails ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fail", ["outage", "rate_limited", "server_error", "timeout"])
def test_a_retryable_pull_failure_asks_for_a_retry_and_leaves_the_row_open(client, db, kyc_queue, fake_prembly, fail):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email)
    getattr(fake_prembly, fail)()
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    with pytest.raises(KycRetry) as retry:
        process_event(db, job, final_try=False)
    assert 0 < retry.value.delay_seconds <= kyc_events.MAX_RETRY_SECONDS
    assert ledger(db, job).processed_at is None and snapshot(fresh(db, a))[1] is None

    fake_prembly.recover()  # the retry
    assert process_event(db, job, final_try=False).outcome == "applied"


def test_the_last_try_closes_the_row_as_an_error_and_never_raises(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.outage()
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    assert process_event(db, job, final_try=True).outcome == "error"
    assert ledger(db, job).outcome == "error" and fresh(db, a).status == "awaiting_result"


@pytest.mark.parametrize("fail", ["wallet_empty", "unauthorised"])
def test_a_pull_failure_retrying_cannot_fix_is_an_error_at_once_and_is_logged_for_ops(client, db, kyc_queue, fake_prembly, caplog, fail):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    getattr(fake_prembly, fail)()
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    assert process_event(db, job, final_try=False).outcome == "error"
    assert "ops attention" in caplog.text


def test_a_watchlisted_code_on_the_pull_closes_the_row_as_an_error_and_taints_nothing(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script(httpx.Response(200, json={"status": False, "response_code": "07", "message": "blocked"}))
    assert process_event(db, deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)).outcome == "error"
    assert fresh(db, a).reason_codes == []


# --- privacy ----------------------------------------------------------------------------------------------------------
def test_nothing_the_provider_says_about_the_person_is_stored_or_logged(client, db, kyc_queue, fake_prembly, caplog):
    caplog.set_level(logging.DEBUG)
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session(
        "sdk_session_a", email=caregiver.email, full_name="SENTINEL FULL NAME", phone="+2340000000SENT", face_confidence=93,
        payload={"face_data": {"confidence": 93}, "verification_response": {"number": SANDBOX_NIN, "photo": SENTINEL_PHOTO,
                                                                            "address": "SENTINEL ADDRESS"},
                 "verification": {"reference": "prem-ref-1"}})
    payload = webhook_payload("sdk_session_a", oakly_ref=str(a.reference))
    payload["data"]["verification_response"] = {"number": SANDBOX_NIN, "photo": SENTINEL_PHOTO}
    body, headers = signed_webhook(payload)
    assert client.post(URL, content=body, headers=headers).status_code == 200
    assert process_event(db, kyc_queue.jobs[-1]).outcome == "applied"

    dumped = ""
    for table in ("identity_verifications", "kyc_webhook_events", "identity_verification_events"):
        dumped += "".join(r[0] for r in db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")))
    dumped += json.dumps(kyc_queue.jobs) + caplog.text
    for sentinel in (SANDBOX_NIN, "SENTINEL", "base64", "photo", "address", "+2340000000"):
        assert sentinel not in dumped, sentinel


# --- the ARQ job ------------------------------------------------------------------------------------------------------
def test_the_job_is_registered_without_a_kept_result():
    registered = {f.name: f for f in worker.WorkerSettings.functions if hasattr(f, "name")}
    assert registered["kyc_process_event"].keep_result_s == 0
    assert registered["kyc_process_event"].coroutine is worker.kyc_process_event


def test_the_job_runs_process_event_and_returns_the_outcome(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email)
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    assert asyncio.run(worker.kyc_process_event({"job_try": 1}, job)) == "applied"


def test_the_job_retries_with_a_delay_then_gives_up_quietly(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.outage()
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    with pytest.raises(Retry) as retry:
        asyncio.run(worker.kyc_process_event({"job_try": 1}, job))
    assert retry.value.defer_score > 0
    last = worker.WorkerSettings.max_tries
    assert asyncio.run(worker.kyc_process_event({"job_try": last}, job)) == "error"


# --- LOCK ORDER: caregiver first, then its attempts ---------------------------------------------------------------------
def _in_thread(fn):
    box = {}

    def run():
        s = SessionLocal()
        try:
            box["result"] = fn(s)
            s.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            box["error"] = exc
            s.rollback()
        finally:
            s.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def _is_waiting_on_a_lock(timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as probe:
            if probe.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")).scalar():
                return True
        time.sleep(0.05)
    return False


def test_erasure_holding_the_caregiver_makes_the_worker_wait_and_then_find_the_attempt_finished(client, db, kyc_queue, fake_prembly):
    """Erasure takes user -> caregiver -> attempts and expires an in-flight attempt. The worker queues behind it on the CAREGIVER row
    and, once it gets in, re-reads the attempt: it is expired, so nothing is written (in particular no session id is put back)."""
    from app.services.account_erasure import AccountErasureService

    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id="sdk_session_a")
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, face_confidence=93)
    job = deliver(client, kyc_queue, "sdk_session_a", ref=a.reference)
    user_id, caregiver_id, attempt_id = caregiver.user_id, caregiver.id, a.id

    eraser = SessionLocal()
    try:
        AccountErasureService(eraser).erase(user_id)  # holds the user, caregiver and attempt locks, uncommitted
        worker_thread, box = _in_thread(lambda s: process_event(s, job))
        assert _is_waiting_on_a_lock(), "the worker should be waiting on the erasure's lock"
        assert worker_thread.is_alive()
        eraser.commit()
    finally:
        eraser.close()
    worker_thread.join(timeout=15)
    assert not worker_thread.is_alive() and "error" not in box, box.get("error")

    assert box["result"].outcome == "ignored_terminal"
    after = fresh(db, a)
    assert (after.status, after.provider_session_id, after.face_confidence, after.provider_reference) == ("expired", None, None, None)
    assert fresh(db, db.get(Caregiver, caregiver_id)).full_name == "Deleted user"


def test_the_worker_holding_its_locks_makes_erasure_wait_and_erasure_wins_without_a_deadlock(client, db, kyc_queue, fake_prembly):
    """The other direction: the worker is inside its locked section (caregiver, then attempt) when erasure starts. Erasure waits on
    the caregiver row, the worker finishes and commits, erasure then wipes what the worker wrote. Two sessions, opposite starting
    points, one lock order: they cannot deadlock."""
    from app.services.account_erasure import AccountErasureService

    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, face_confidence=93)
    user_id, attempt_id = caregiver.user_id, a.id

    worker_db = SessionLocal()
    try:
        locked_caregiver, locked_attempt = lock_caregiver_then_attempt(worker_db, attempt_id)
        assert locked_attempt.status == "awaiting_result"
        eraser_thread, box = _in_thread(lambda s: AccountErasureService(s).erase(user_id))
        assert _is_waiting_on_a_lock(), "erasure should be waiting on the worker's caregiver lock"
        outcome, _ = kyc_events.confirm_and_record(worker_db, attempt_id, fake_prembly_session(fake_prembly, "sdk_session_a"))
        assert outcome == "applied"
        worker_db.commit()
    finally:
        worker_db.close()
    eraser_thread.join(timeout=15)
    assert not eraser_thread.is_alive() and "error" not in box, box.get("error")

    after = fresh(db, a)
    assert (after.status, after.provider_session_id) == ("expired", None)  # what the worker bound is gone with the erasure


def fake_prembly_session(fake, session_id):
    from app.services.prembly import PremblyClient

    return PremblyClient.from_settings().get_session(session_id, include_payload=True)


def test_two_workers_on_one_attempt_do_not_both_record(client, db, kyc_queue, fake_prembly):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_prembly.script_session("sdk_session_a", email=caregiver.email, face_confidence=93)
    jobs = [deliver(client, kyc_queue, "sdk_session_a", ref=a.reference) for _ in range(3)]
    threads = [_in_thread(lambda s, j=j: process_event(s, j).outcome) for j in jobs]
    for t, _ in threads:
        t.join(timeout=30)
    assert [b.get("error") for _, b in threads] == [None, None, None]
    assert [b["result"] for _, b in threads] == ["applied"] * 3  # the same pull, recorded idempotently
    assert float(fresh(db, a).face_confidence) == 93.0


def _lock_sequence(run):
    """The tables locked ``FOR UPDATE``, in the order the statements were issued while ``run()`` executed."""
    from sqlalchemy import event

    from app.db.session import engine

    seen: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            seen.append(re.search(r"FROM\s+(\w+)", statement).group(1))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        run()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return seen


def test_both_paths_issue_the_caregiver_lock_before_the_attempt_lock(client, db, kyc_queue, fake_prembly):
    """The deterministic half of the lock-order proof (the two race tests above prove the outcomes): in the worker and in erasure the
    caregiver row is locked first and the attempts after it. Swapping the two in either place makes this fail."""
    from app.services.account_erasure import AccountErasureService

    caregiver = cg(db)
    a = attempt(db, caregiver)
    user_id, attempt_id = caregiver.user_id, a.id

    worker_order = _lock_sequence(lambda: (lock_caregiver_then_attempt(db, attempt_id), db.rollback()))
    assert worker_order == ["caregivers", "identity_verifications"]

    erasure_order = _lock_sequence(lambda: (AccountErasureService(db).erase(user_id), db.rollback()))
    assert erasure_order.index("caregivers") < erasure_order.index("identity_verifications")
