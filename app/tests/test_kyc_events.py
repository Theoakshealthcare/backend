"""K3/KD1: the worker side of the Dojah webhook. Locate the attempt by its minted reference, bind it, PULL the verification, record it.

Since K4 the same locked section also DECIDES (kyc_engine; the rules and the decision matrix are tested in test_kyc_decision.py and
test_kyc_engine.py), so a final pull moves the attempt and the caregiver. These tests keep their K3 job: binding, trust, idempotency,
retries and lock order. Design: docs/plans/2026-10-27-caregiver-kyc-design.md sections 8.3, 8.4 and 16.1; the Dojah delta. Names,
ids and numbers are placeholders.
"""
import asyncio
import json
import logging
import re
import threading
import time
import uuid
from datetime import date, datetime

import httpx
import pytest
from arq import Retry
from sqlalchemy import text

from app import worker
from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import (
    VERIFICATION_ACTIVE_STATUSES, VERIFICATION_TERMINAL_STATUSES, IdentityVerification, IdentityVerificationEvent, KycWebhookEvent,
)
from app.db.session import SessionLocal
from app.services import kyc_events
from app.services.dojah import DojahClient
from app.services.kyc_events import KycRetry, lock_caregiver_then_attempt, process_event
from app.tests.factories import make_caregiver
from app.tests.fake_dojah import (  # noqa: F401
    SANDBOX_NIN, SENTINEL_PHOTO, FakeDojah, fake_dojah, kyc_queue, new_reference, signed_webhook, webhook_payload,
)

URL = "/api/v1/kyc/dojah/webhook"


@pytest.fixture(autouse=True)
def env(fake_dojah, kyc_queue):  # noqa: F811
    return fake_dojah


def cg(db, **kw):
    kw.setdefault("date_of_birth", date(1990, 4, 12))
    return make_caregiver(db, approved=False, profile_completion_step=2, **kw)


def attempt(db, caregiver, status="awaiting_result", **kw):
    """An attempt as ``start_session`` leaves it: with the reference the server minted. ``a.provider_session_id`` is that reference."""
    kw.setdefault("provider_session_id", new_reference())
    row = IdentityVerification(caregiver_id=caregiver.id, attempt_no=kw.pop("attempt_no", 1), status=status, **kw)
    db.add(row)
    db.commit()
    return row


def deliver(client, kyc_queue, reference, *, status="Completed", entity=None, **extra):
    """POST a signed webhook through the real endpoint; returns the job the endpoint queued."""
    before = len(kyc_queue.jobs)
    body, headers = signed_webhook(webhook_payload(reference, status=status, entity=entity, **extra))
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


def pulled(reference):
    return DojahClient.from_settings().get_verification(reference)


# --- a pull-confirmed result is recorded and decided ------------------------------------------------------------------
def test_a_confirmed_result_is_recorded_from_the_pull_and_decided_in_the_same_section(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    fake_dojah.script_clean_pass(ref, caregiver, confidence=92.5)
    job = deliver(client, kyc_queue, ref)

    result = process_event(db, job)

    assert result.outcome == "applied" and result.verification_id == a.id and result.observation.verdict == "pass"
    assert result.notice is not None and result.notice.kind == "verified"
    a = fresh(db, a)
    assert a.provider_session_id == ref  # unchanged: the reference the server minted
    assert (float(a.face_confidence), a.liveness_confidence, a.provider_cost, a.provider_reference) == (92.5, None, None, None)
    assert a.completed_at is not None and a.reason_codes == []
    assert (a.status, a.decided_via, a.face_threshold, a.id_type) == ("passed", "provider", 85, "nin")
    assert (a.name_match, a.dob_match) == (True, True) and a.id_fingerprint and SANDBOX_NIN not in a.id_fingerprint
    c = fresh(db, caregiver)
    assert (c.identity_verification_status, c.profile_completion_step, c.profile_status) == ("verified", 3, "under_review")
    (trail,) = db.query(IdentityVerificationEvent).all()
    assert (trail.actor, trail.from_status, trail.to_status, trail.reason_codes) == ("provider", "awaiting_result", "passed", [])
    event = ledger(db, job)
    assert (event.outcome, event.verification_id) == ("applied", a.id) and event.processed_at is not None


def test_the_pull_uses_the_secret_key_and_the_attempts_own_reference(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    process_event(db, deliver(client, kyc_queue, a.provider_session_id))
    (request,) = fake_dojah.requests
    assert request.url.params["reference_id"] == a.provider_session_id
    assert request.headers["authorization"] and request.headers["appid"]


@pytest.mark.parametrize("status", VERIFICATION_ACTIVE_STATUSES)
def test_every_active_status_can_be_decided(client, db, kyc_queue, fake_dojah, status):
    caregiver = cg(db)
    a = attempt(db, caregiver, status=status)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    assert process_event(db, deliver(client, kyc_queue, a.provider_session_id)).outcome == "applied"
    assert fresh(db, a).status == "passed"


@pytest.mark.parametrize("status", ["Pending", "Ongoing", "Abandoned"])
def test_a_verification_that_is_not_final_records_nothing(client, db, kyc_queue, fake_dojah, status):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_verification(a.provider_session_id, status=status, confidence=95)
    result = process_event(db, deliver(client, kyc_queue, a.provider_session_id, status=status))
    after = fresh(db, a)
    assert result.outcome == "applied" and result.observation.final is False and result.notice is None
    assert (after.status, after.face_confidence, after.completed_at, after.reason_codes) == ("awaiting_result", None, None, [])
    assert fresh(db, caregiver).identity_verification_status == "pending" and db.query(IdentityVerificationEvent).count() == 0


def test_a_completed_flow_the_pull_does_not_show_yet_is_retried_not_consumed(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    fake_dojah.script_verification(ref, status="Ongoing")
    job = deliver(client, kyc_queue, ref, status="Completed")  # the webhook says the flow ended
    with pytest.raises(KycRetry):
        process_event(db, job, final_try=False)
    assert ledger(db, job).processed_at is None and fresh(db, a).completed_at is None  # nothing was kept

    fake_dojah.script_clean_pass(ref, caregiver)  # the pull caught up
    assert process_event(db, job, final_try=False).outcome == "applied"
    assert fresh(db, a).completed_at is not None


def test_on_the_last_try_a_still_unfinished_verification_is_recorded_as_nothing(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_verification(a.provider_session_id, status="Pending")
    job = deliver(client, kyc_queue, a.provider_session_id, status="Completed")
    assert process_event(db, job, final_try=True).outcome == "applied"
    assert fresh(db, a).completed_at is None


def test_a_score_outside_its_scale_is_dropped_not_clamped_and_a_person_looks(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver, confidence=140)
    process_event(db, deliver(client, kyc_queue, a.provider_session_id))
    a = fresh(db, a)
    assert a.face_confidence is None and a.completed_at is not None
    assert (a.status, a.reason_codes) == ("needs_review", ["identity_data_missing"])  # no score is evidence we did not see


def test_completed_is_not_a_pass(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver, government=False)
    result = process_event(db, deliver(client, kyc_queue, a.provider_session_id))
    assert result.observation.verdict == "fail" and fresh(db, a).status == "failed"
    assert fresh(db, caregiver).identity_verification_status == "in_progress"  # try again, never failed by a machine


# --- trust: the webhook alone is never believed -----------------------------------------------------------------------
def test_a_webhook_failure_over_a_pulled_pass_is_a_disagreement_that_goes_to_review_not_to_verified(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    job = deliver(client, kyc_queue, a.provider_session_id, status="Failed")
    assert process_event(db, job).outcome == "disagreement"
    a = fresh(db, a)
    assert a.reason_codes == ["provider_disagreement"] and (a.status, a.decided_via) == ("needs_review", "provider")
    assert ledger(db, job).outcome == "disagreement"
    assert fresh(db, caregiver).identity_verification_status == "needs_review"  # a webhook can never talk a pull into "verified"


def test_a_webhook_that_carries_a_pass_cannot_make_one(client, db, kyc_queue, fake_dojah):
    """The signed body even carries a perfect ``data`` block; Dojah's own record (the pull) is a failed selfie: the pull wins."""
    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    fake_dojah.script_clean_pass(ref, caregiver, selfie=False, confidence=30)
    perfect = FakeDojah.entity(ref, confidence=99)
    job = deliver(client, kyc_queue, ref, status="Completed", entity=perfect)
    assert process_event(db, job).outcome == "applied"
    a = fresh(db, a)
    assert a.status == "failed" and "face_mismatch" in a.reason_codes
    assert fresh(db, caregiver).identity_verification_status == "in_progress"


def test_a_pulled_answer_about_another_reference_is_never_used(client, db, kyc_queue, fake_dojah):
    """The pull is made by the attempt's own reference, and what comes back must name it. An answer carrying someone else's perfect
    run decides nothing and taints nothing."""
    caregiver = cg(db)
    a = attempt(db, caregiver)
    stranger = new_reference()
    fake_dojah.script_clean_pass(stranger, caregiver, confidence=99)
    fake_dojah.script(httpx.Response(200, json={"entity": fake_dojah.verifications[stranger]}))
    job = deliver(client, kyc_queue, a.provider_session_id)
    assert process_event(db, job).outcome == "error"
    after = fresh(db, a)
    assert (after.status, after.reason_codes, after.face_confidence, after.completed_at) == ("awaiting_result", [], None, None)
    assert fresh(db, caregiver).identity_verification_status == "pending"


def test_a_verification_that_does_not_name_the_attempts_reference_decides_nothing_inside_the_lock(client, db, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    stranger = new_reference()
    fake_dojah.script_clean_pass(stranger, caregiver, confidence=99)
    recorded = kyc_events.confirm_and_record(db, a.id, pulled(stranger))
    db.commit()
    assert (recorded.outcome, recorded.observation, recorded.notice) == ("disagreement", None, None)
    after = fresh(db, a)
    assert (after.status, after.reason_codes, after.face_confidence, after.completed_at) == ("awaiting_result", [], None, None)


def test_an_attempt_without_a_reference_cannot_be_decided(client, db, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver, provider_session_id=None)
    ref = new_reference()
    fake_dojah.script_clean_pass(ref, caregiver)
    assert kyc_events.confirm_and_record(db, a.id, pulled(ref)).outcome == "disagreement"
    assert fresh(db, a).completed_at is None


def test_a_signed_webhook_for_a_reference_dojah_does_not_know_is_a_disagreement(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)  # nothing scripted: the pull answers 404
    job = deliver(client, kyc_queue, a.provider_session_id)
    assert process_event(db, job).outcome == "disagreement"
    assert fresh(db, a).reason_codes == [] and ledger(db, job).processed_at is not None  # ledger only; the attempt is not tainted


def test_the_binding_is_an_exact_match_of_the_reference(client, db, kyc_queue, fake_dojah):
    """A neighbour's reference (one hex digit off) finds no attempt and costs no pull."""
    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    near = ref[:-1] + ("0" if ref[-1] != "0" else "1")
    assert re.fullmatch(r"OKL-[0-9a-f]{32}", near) and near != ref
    fake_dojah.script_clean_pass(near, caregiver)
    assert process_event(db, deliver(client, kyc_queue, near)).outcome == "orphan"
    assert fresh(db, a).completed_at is None and fake_dojah.requests == []


# --- orphans, terminal attempts, replays ------------------------------------------------------------------------------
def test_an_orphan_event_is_recorded_without_a_pull(client, db, kyc_queue, fake_dojah):
    job = deliver(client, kyc_queue, new_reference())
    result = process_event(db, job)
    assert result.outcome == "orphan" and result.verification_id is None and fake_dojah.requests == []
    assert (ledger(db, job).outcome, ledger(db, job).verification_id) == ("orphan", None)


@pytest.mark.parametrize("status", VERIFICATION_TERMINAL_STATUSES)
def test_a_terminal_attempt_ignores_events_and_costs_no_pull(client, db, kyc_queue, fake_dojah, status):
    caregiver = cg(db)
    a = attempt(db, caregiver, status=status, decided_via="admin" if status == "passed" else None)
    before = snapshot(a)
    fake_dojah.script_verification(a.provider_session_id, status="Failed")
    job = deliver(client, kyc_queue, a.provider_session_id, status="Failed")
    assert process_event(db, job).outcome == "ignored_terminal"
    assert fake_dojah.requests == [] and snapshot(fresh(db, a)) == before
    assert ledger(db, job).outcome == "ignored_terminal"


def test_a_later_event_for_a_decided_attempt_changes_nothing_the_second_time(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    fake_dojah.script_clean_pass(ref, caregiver)
    first = deliver(client, kyc_queue, ref, status="Completed")
    assert process_event(db, first).outcome == "applied"
    recorded = snapshot(fresh(db, a))
    later = deliver(client, kyc_queue, ref, status="Failed")  # a different status is a different delivery (kyc_seam D2)
    assert later["event_id"] != first["event_id"]
    again = process_event(db, later)
    assert again.outcome == "ignored_terminal" and again.notice is None  # the attempt is finished: inert, nobody is told twice
    assert snapshot(fresh(db, a)) == recorded
    assert len(fake_dojah.requests) == 1  # a finished attempt costs no pull
    assert db.query(IdentityVerificationEvent).count() == 1


def test_a_recorded_result_is_never_overwritten_inside_the_lock(client, db, kyc_queue, fake_dojah):
    """The pre-check in process_event is unlocked; a second worker that got past it must still find the result and leave it."""
    caregiver = cg(db)
    a = attempt(db, caregiver, face_confidence=91, completed_at=datetime(2026, 10, 27, 10, 0))
    fake_dojah.script_verification(a.provider_session_id, confidence=12, selfie=False)
    recorded = kyc_events.confirm_and_record(db, a.id, pulled(a.provider_session_id))
    assert (recorded.outcome, recorded.observation, recorded.notice) == ("applied", None, None)
    db.commit()
    after = fresh(db, a)
    assert float(after.face_confidence) == 91.0 and after.completed_at == datetime(2026, 10, 27, 10, 0)


def test_a_job_delivered_twice_is_processed_once(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_verification(a.provider_session_id, status="Pending")
    job = deliver(client, kyc_queue, a.provider_session_id, status="Pending")
    assert process_event(db, job).outcome == "applied"
    assert process_event(db, job).outcome == "applied"
    assert len(fake_dojah.requests) == 1


def test_a_missing_ledger_row_is_not_an_error(db):
    assert process_event(db, {"event_id": str(uuid.uuid4())}).outcome == "error"


def test_a_ledger_row_whose_reference_was_erased_is_an_orphan(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    job = deliver(client, kyc_queue, a.provider_session_id)
    db.query(KycWebhookEvent).update({"provider_session_id": None})
    db.commit()
    assert process_event(db, job).outcome == "orphan" and fake_dojah.requests == []


# --- the pull fails ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fail", ["outage", "rate_limited", "server_error", "timeout"])
def test_a_retryable_pull_failure_asks_for_a_retry_and_leaves_the_row_open(client, db, kyc_queue, fake_dojah, fail):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    getattr(fake_dojah, fail)()
    job = deliver(client, kyc_queue, a.provider_session_id)
    with pytest.raises(KycRetry) as retry:
        process_event(db, job, final_try=False)
    assert 0 < retry.value.delay_seconds <= kyc_events.MAX_RETRY_SECONDS
    assert ledger(db, job).processed_at is None and fresh(db, a).completed_at is None

    fake_dojah.recover()  # the retry
    assert process_event(db, job, final_try=False).outcome == "applied"


def test_the_last_try_closes_the_row_as_an_error_and_never_raises(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.outage()
    job = deliver(client, kyc_queue, a.provider_session_id)
    assert process_event(db, job, final_try=True).outcome == "error"
    assert ledger(db, job).outcome == "error" and fresh(db, a).status == "awaiting_result"


@pytest.mark.parametrize("fail", ["wallet_empty", "unauthorised"])
def test_a_pull_failure_retrying_cannot_fix_is_an_error_at_once_and_is_logged_for_ops(client, db, kyc_queue, fake_dojah, caplog, fail):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    getattr(fake_dojah, fail)()
    job = deliver(client, kyc_queue, a.provider_session_id)
    assert process_event(db, job, final_try=False).outcome == "error"
    assert "ops attention" in caplog.text


def test_another_rejection_closes_the_row_as_an_error_and_taints_nothing(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script(httpx.Response(422, json={"error": "bad reference"}))
    assert process_event(db, deliver(client, kyc_queue, a.provider_session_id)).outcome == "error"
    assert fresh(db, a).reason_codes == []


# --- privacy ----------------------------------------------------------------------------------------------------------
def test_nothing_the_provider_says_about_the_person_is_stored_or_logged(client, db, kyc_queue, fake_dojah, caplog):
    caplog.set_level(logging.DEBUG)
    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    fake_dojah.script_clean_pass(ref, caregiver, confidence=93, first_name="SENTINELFIRST", last_name="SENTINELLAST")
    entity = FakeDojah.entity(ref, first_name="SENTINELFIRST", last_name="SENTINELLAST")
    body, headers = signed_webhook(webhook_payload(ref, entity=entity))
    assert client.post(URL, content=body, headers=headers).status_code == 200
    process_event(db, kyc_queue.jobs[-1])

    dumped = ""
    for table in ("identity_verifications", "kyc_webhook_events", "identity_verification_events"):
        dumped += "".join(r[0] for r in db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")))
    dumped += json.dumps(kyc_queue.jobs) + caplog.text
    for sentinel in (SANDBOX_NIN, "SENTINEL", SENTINEL_PHOTO, "base64", "images.dojah.invalid", "12-Apr"):
        assert sentinel not in dumped, sentinel


# --- the ARQ job ------------------------------------------------------------------------------------------------------
def test_the_job_is_registered_without_a_kept_result():
    registered = {f.name: f for f in worker.WorkerSettings.functions if hasattr(f, "name")}
    assert registered["kyc_process_event"].keep_result_s == 0
    assert registered["kyc_process_event"].coroutine is worker.kyc_process_event


def test_the_job_runs_process_event_and_returns_the_outcome(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver)
    job = deliver(client, kyc_queue, a.provider_session_id)
    assert asyncio.run(worker.kyc_process_event({"job_try": 1}, job)) == "applied"


def test_the_job_retries_with_a_delay_then_gives_up_quietly(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.outage()
    job = deliver(client, kyc_queue, a.provider_session_id)
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


def test_erasure_holding_the_caregiver_makes_the_worker_wait_and_then_find_the_attempt_finished(client, db, kyc_queue, fake_dojah):
    """Erasure takes user -> caregiver -> attempts and expires an in-flight attempt. The worker queues behind it on the CAREGIVER row
    and, once it gets in, re-reads the attempt: it is expired, so nothing is written (in particular no reference is put back)."""
    from app.services.account_erasure import AccountErasureService

    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver, confidence=93)
    job = deliver(client, kyc_queue, a.provider_session_id)
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


def test_the_worker_holding_its_locks_makes_erasure_wait_and_erasure_wins_without_a_deadlock(client, db, kyc_queue, fake_dojah):
    """The other direction: the worker is inside its locked section (caregiver, then attempt) when erasure starts. Erasure waits on
    the caregiver row, the worker finishes and commits, erasure then wipes what the worker wrote. Two sessions, opposite starting
    points, one lock order: they cannot deadlock."""
    from app.services.account_erasure import AccountErasureService

    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    fake_dojah.script_verification(ref, confidence=93)
    user_id, attempt_id = caregiver.user_id, a.id

    worker_db = SessionLocal()
    try:
        locked_caregiver, locked_attempt = lock_caregiver_then_attempt(worker_db, attempt_id)
        assert locked_attempt.status == "awaiting_result"
        eraser_thread, box = _in_thread(lambda s: AccountErasureService(s).erase(user_id))
        assert _is_waiting_on_a_lock(), "erasure should be waiting on the worker's caregiver lock"
        recorded = kyc_events.confirm_and_record(worker_db, attempt_id, pulled(ref))
        assert recorded.outcome == "applied"
        worker_db.commit()
    finally:
        worker_db.close()
    eraser_thread.join(timeout=15)
    assert not eraser_thread.is_alive() and "error" not in box, box.get("error")

    after = fresh(db, a)
    # The worker decided first (no registry data in this session, so a person looks), then erasure scrubbed what it bound and wrote
    assert (after.status, after.provider_session_id, after.id_fingerprint, after.provider_reference) == ("needs_review", None, None, None)


def test_two_workers_on_one_attempt_do_not_both_record(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    fake_dojah.script_clean_pass(a.provider_session_id, caregiver, confidence=93)
    jobs = [deliver(client, kyc_queue, a.provider_session_id, note=n) for n in range(3)]
    threads = [_in_thread(lambda s, j=j: process_event(s, j).outcome) for j in jobs]
    for t, _ in threads:
        t.join(timeout=30)
    assert [b.get("error") for _, b in threads] == [None, None, None]
    assert sorted(b["result"] for _, b in threads) == ["applied", "ignored_terminal", "ignored_terminal"]  # one decides, two find it done
    after = fresh(db, a)
    assert (after.status, float(after.face_confidence)) == ("passed", 93.0)
    assert db.query(IdentityVerificationEvent).count() == 1  # decided once


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


def test_both_paths_issue_the_caregiver_lock_before_the_attempt_lock(client, db, kyc_queue, fake_dojah):
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


def test_a_late_webhook_after_erasure_does_not_write_the_erased_reference_back(client, db, kyc_queue, fake_dojah):
    caregiver = cg(db)
    a = attempt(db, caregiver)
    ref = a.provider_session_id
    a.provider_session_id = a.provider_reference = None  # what erasure does
    db.commit()
    job = deliver(client, kyc_queue, ref)  # the signed, well-formed webhook still arrives
    assert db.get(KycWebhookEvent, uuid.UUID(job["event_id"])).provider_session_id == ref  # held only until the worker looks at it
    assert process_event(db, job).outcome == "orphan" and fake_dojah.requests == []
    assert ledger(db, job).provider_session_id is None
