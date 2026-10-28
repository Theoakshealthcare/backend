"""K3: the public Prembly webhook. Signature over the raw body, the token ledger, enqueue compensation, the body cap.

Design: docs/plans/2026-10-27-caregiver-kyc-design.md section 8.1 and 16.1. Every key, token and id here is a generated placeholder.
"""
import json
import logging
import uuid

import pytest

from app.core.config import settings
from app.db.models.identity_verification import KycWebhookEvent
from app.services import kyc_seam as seam
from app.tests.fake_prembly import (
    TEST_WEBHOOK_KEY, body_bytes, fake_prembly, kyc_queue, sign, signed_webhook, webhook_payload,  # noqa: F401
)

URL = "/api/v1/kyc/prembly/webhook"
SENTINEL = "SENTINEL-REGISTRY-RECORD-do-not-store"


@pytest.fixture(autouse=True)
def env(fake_prembly, kyc_queue):  # noqa: F811
    return kyc_queue


def post(client, payload=None, *, body=None, headers=None, key=TEST_WEBHOOK_KEY, token=None):
    if body is None:
        body, signed = signed_webhook(payload if payload is not None else webhook_payload("sdk_session_a"), key, token=token)
        headers = signed if headers is None else headers
    return client.post(URL, content=body, headers=headers or {})


def rows(db):
    db.expire_all()
    return db.query(KycWebhookEvent).all()


# --- the happy path ---------------------------------------------------------------------------------------------------
def test_a_signed_event_is_recorded_and_queued(client, db, kyc_queue):
    ref = str(uuid.uuid4())
    r = post(client, webhook_payload("sdk_session_a", oakly_ref=ref), token="tok-1")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    (row,) = rows(db)
    assert (row.token, row.provider_session_id, row.event_type, row.signature_ok) == ("tok-1", "sdk_session_a",
                                                                                     "verification.completed", True)
    assert row.processed_at is None and row.outcome is None and row.verification_id is None and len(row.body_sha256) == 64
    assert kyc_queue.jobs == [{"event_id": str(row.id), "session_id": "sdk_session_a", "oakly_ref": ref, "claim": "pass"}]


def test_the_route_is_public_and_hidden_from_the_schema(client):
    assert URL.removeprefix("/api/v1") not in client.get("/api/v1/openapi.json").json()["paths"]


def test_the_body_is_not_stored_anywhere(client, db):
    payload = webhook_payload("sdk_session_a")
    payload["data"]["verification_response"] = {"first_name": SENTINEL, "photo": "data:image/jpeg;base64," + SENTINEL}
    assert post(client, payload).status_code == 200
    text = json.dumps([{c.name: str(getattr(r, c.name)) for c in r.__table__.columns} for r in rows(db)])
    assert SENTINEL not in text


@pytest.mark.parametrize("where", ["metadata", "data.metadata", "data.widget_info.user_ref"])
def test_our_reference_is_read_from_wherever_prembly_echoes_it(client, kyc_queue, where):
    ref = str(uuid.uuid4())
    payload = webhook_payload("sdk_session_a")
    if where == "metadata":
        payload["metadata"] = {"oakly_ref": ref}
    elif where == "data.metadata":
        payload["data"]["metadata"] = {"oakly_ref": ref}
    else:
        payload["data"]["widget_info"]["user_ref"] = ref
    assert post(client, payload).status_code == 200
    assert kyc_queue.jobs[0]["oakly_ref"] == ref


def test_a_reference_that_is_not_a_uuid_is_ignored(client, kyc_queue):
    payload = webhook_payload("sdk_session_a")
    payload["metadata"] = {"oakly_ref": "not-a-uuid"}
    assert post(client, payload).status_code == 200 and kyc_queue.jobs[0]["oakly_ref"] is None


def test_a_failed_verdict_is_carried_as_the_claim(client, kyc_queue):
    assert post(client, webhook_payload("sdk_session_a", status="FAILED")).status_code == 200
    assert kyc_queue.jobs[0]["claim"] == "fail"


def test_an_unknown_event_type_is_acknowledged_and_handled_like_any_other(client, db, kyc_queue):
    # Only ``verification.completed`` is documented. Names are never trusted: the pull decides (kyc_seam U5).
    assert post(client, webhook_payload("sdk_session_a", event="session.something_new")).status_code == 200
    assert rows(db)[0].event_type == "session.something_new" and len(kyc_queue.jobs) == 1


@pytest.mark.parametrize("event", ["has spaces and <script>", "x" * 65, "", 7, None, ["a"]])
def test_an_unusable_event_type_is_stored_as_null_but_still_handled(client, db, kyc_queue, event):
    payload = webhook_payload("sdk_session_a")
    payload["event"] = event
    assert post(client, payload).status_code == 200
    assert rows(db)[0].event_type is None and len(kyc_queue.jobs) == 1


# --- signature --------------------------------------------------------------------------------------------------------
def test_a_bad_signature_is_refused_and_nothing_is_stored_or_queued(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload("sdk_session_a"), "some-other-key")
    r = client.post(URL, content=body, headers=headers)
    assert r.status_code == 401 and not kyc_queue.jobs and rows(db) == []


def test_a_missing_signature_is_refused(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload("sdk_session_a"))
    del headers["x-prembly-signature"]
    assert client.post(URL, content=body, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


@pytest.mark.parametrize("header", ["", " ", "sha256=abcd", "abcd", "zz==", "0" * 64])
def test_malformed_signature_headers_are_refused(client, db, kyc_queue, header):
    body, headers = signed_webhook(webhook_payload("sdk_session_a"))
    headers["x-prembly-signature"] = header
    assert client.post(URL, content=body, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


def test_a_tampered_body_is_refused(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload("sdk_session_a"))
    tampered = body.replace(b"sdk_session_a", b"sdk_session_b")
    assert tampered != body
    assert client.post(URL, content=tampered, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


def test_a_re_serialised_body_does_not_verify(client, kyc_queue):
    """The signature is over the exact bytes: the same JSON with different spacing is a different message."""
    payload = webhook_payload("sdk_session_a")
    body, headers = signed_webhook(payload)
    respaced = json.dumps(payload).encode()  # default separators: valid JSON, different bytes
    assert respaced != body
    assert client.post(URL, content=respaced, headers=headers).status_code == 401 and not kyc_queue.jobs


def test_an_empty_key_never_verifies_even_for_a_signature_made_with_it(client, db, kyc_queue, monkeypatch):
    monkeypatch.setattr(settings, "PREMBLY_WEBHOOK_KEY", "")
    body, headers = signed_webhook(webhook_payload("sdk_session_a"), key="")  # anyone can compute this
    assert client.post(URL, content=body, headers=headers).status_code == 401
    body, headers = signed_webhook(webhook_payload("sdk_session_a"), key=TEST_WEBHOOK_KEY)
    assert client.post(URL, content=body, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


def test_verify_signature_unit():
    body = b'{"a":1}'
    good = sign(body, "k")
    assert seam.verify_signature(body, good, "k") and seam.verify_signature(body, f" {good}\n", "k")
    assert not seam.verify_signature(body, good, "")
    assert not seam.verify_signature(body, good, "other")
    assert not seam.verify_signature(body, None, "k") and not seam.verify_signature(body, "", "k")
    assert not seam.verify_signature(body + b" ", good, "k")
    assert not seam.verify_signature(body, "\u00e9\u00e9", "k")  # non-ASCII text: refused, not an error
    assert not seam.verify_signature(body, sign(body, "").strip(), "k")


# --- token, JSON and payload ------------------------------------------------------------------------------------------
def test_a_missing_token_is_untrusted(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload("sdk_session_a"))
    del headers["token"]
    assert client.post(URL, content=body, headers=headers).status_code == 400
    assert not kyc_queue.jobs and rows(db) == []


@pytest.mark.parametrize("token", ["", " ", "x" * 201, "bad\ttoken", " padded "])
def test_a_malformed_token_is_untrusted(client, db, kyc_queue, token):
    body, headers = signed_webhook(webhook_payload("sdk_session_a"))
    headers["token"] = token
    assert client.post(URL, content=body, headers=headers).status_code == 400
    assert not kyc_queue.jobs and rows(db) == []


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe", b"[]", b'"x"', b"null", b"{}", b'{"event":"x"}',
                                 b'{"session_id":""}', b'{"session_id":7}', b'{"session_id":"../../etc"}',
                                 b'{"session_id":"a b"}', b'{"session_id":"abc\\n"}', b"[" * 100_000])
def test_a_signed_body_that_is_not_an_event_is_a_400(client, db, kyc_queue, raw):
    headers = {"x-prembly-signature": sign(raw, TEST_WEBHOOK_KEY), "token": uuid.uuid4().hex}
    assert client.post(URL, content=raw, headers=headers).status_code == 400
    assert not kyc_queue.jobs and rows(db) == []


# --- dedupe and replay ------------------------------------------------------------------------------------------------
def test_a_duplicate_token_is_acknowledged_once_and_queued_once(client, db, kyc_queue):
    payload = webhook_payload("sdk_session_a")
    assert post(client, payload, token="same").status_code == 200
    assert post(client, payload, token="same").status_code == 200  # Prembly retried; we already have it
    assert len(rows(db)) == 1 and len(kyc_queue.jobs) == 1


def test_a_replayed_body_under_a_new_token_is_a_new_event_that_the_worker_will_find_inert(client, db, kyc_queue):
    """No timestamp exists to bound a replay (kyc_seam U4). Transport dedupe is by token; state-level inertness (a terminal attempt
    ignores events, a finished attempt costs no pull) is tested in test_kyc_events."""
    body, headers = signed_webhook(webhook_payload("sdk_session_a"))
    for token in ("t1", "t2"):
        assert client.post(URL, content=body, headers={**headers, "token": token}).status_code == 200
    assert len(rows(db)) == 2 and len(kyc_queue.jobs) == 2


def test_a_redelivery_after_a_lost_job_is_queued_again(client, db, kyc_queue):
    """The first delivery died between the insert and the enqueue: its row exists, no job does. Prembly's retry must not be deduped."""
    payload = webhook_payload("sdk_session_a")
    assert post(client, payload, token="lost").status_code == 200
    kyc_queue.jobs.clear()  # the job never reached Redis
    assert post(client, payload, token="lost").status_code == 200
    assert len(rows(db)) == 1 and [j["event_id"] for j in kyc_queue.jobs] == [str(rows(db)[0].id)]


def test_a_redelivery_of_a_processed_event_is_not_queued_again(client, db, kyc_queue):
    payload = webhook_payload("sdk_session_a")
    assert post(client, payload, token="done").status_code == 200
    kyc_queue.jobs.clear()
    from datetime import datetime

    db.query(KycWebhookEvent).update({"processed_at": datetime.utcnow(), "outcome": "orphan"})
    db.commit()
    assert post(client, payload, token="done").status_code == 200 and not kyc_queue.jobs


def test_a_failed_requeue_of_a_redelivery_keeps_the_existing_row(client, db, kyc_queue):
    payload = webhook_payload("sdk_session_a")
    assert post(client, payload, token="keep").status_code == 200
    kyc_queue.jobs.clear()
    kyc_queue.fail = ConnectionError("redis down")
    assert post(client, payload, token="keep").status_code == 500
    assert len(rows(db)) == 1  # not compensated: this request did not create it
    kyc_queue.fail = None
    assert post(client, payload, token="keep").status_code == 200 and len(kyc_queue.jobs) == 1


def test_concurrent_deliveries_of_one_token_record_one_row(client, db, kyc_queue):
    from concurrent.futures import ThreadPoolExecutor

    body, headers = signed_webhook(webhook_payload("sdk_session_a"), token="race")
    with ThreadPoolExecutor(4) as pool:
        statuses = list(pool.map(lambda _: client.post(URL, content=body, headers=headers).status_code, range(4)))
    assert statuses == [200] * 4 and len(rows(db)) == 1 and len(kyc_queue.jobs) == 1


# --- enqueue failure ---------------------------------------------------------------------------------------------------
def test_an_enqueue_failure_undoes_the_row_and_asks_for_a_retry(client, db, kyc_queue):
    kyc_queue.fail = ConnectionError("redis down")
    r = post(client, webhook_payload("sdk_session_a"), token="retry-me")
    assert r.status_code == seam.ENQUEUE_FAILURE_STATUS == 500
    assert rows(db) == [] and not kyc_queue.jobs

    kyc_queue.fail = None  # Prembly's retry, same token: processed, not deduped away
    assert post(client, webhook_payload("sdk_session_a"), token="retry-me").status_code == 200
    assert len(rows(db)) == 1 and len(kyc_queue.jobs) == 1


def test_a_refused_job_id_is_treated_as_a_failed_enqueue(client, db, kyc_queue):
    from app.services.kyc_queue import QueueRefused

    kyc_queue.fail = QueueRefused("already queued")
    assert post(client, webhook_payload("sdk_session_a")).status_code == 500 and rows(db) == []


# --- size cap ---------------------------------------------------------------------------------------------------------
def test_an_oversized_body_is_refused_before_it_is_hashed(client, db, kyc_queue, monkeypatch):
    monkeypatch.setattr(seam, "MAX_BODY_BYTES", 1_000)
    big = b'{"session_id":"s","pad":"' + b"x" * 2_000 + b'"}'
    headers = {"x-prembly-signature": sign(big, TEST_WEBHOOK_KEY), "token": "big"}  # even a VALID signature
    assert client.post(URL, content=big, headers=headers).status_code == 413
    assert not kyc_queue.jobs and rows(db) == []


def test_a_chunked_oversized_body_is_cut_off(client, kyc_queue, monkeypatch):
    monkeypatch.setattr(seam, "MAX_BODY_BYTES", 1_000)

    def chunks():
        for _ in range(10):
            yield b"x" * 400

    assert client.post(URL, content=chunks(), headers={"x-prembly-signature": "x", "token": "t"}).status_code == 413


def test_a_body_at_the_cap_is_still_read(client, kyc_queue, monkeypatch):
    payload = webhook_payload("sdk_session_a")
    size = len(body_bytes(payload))
    monkeypatch.setattr(seam, "MAX_BODY_BYTES", size)
    assert post(client, payload).status_code == 200


# --- logs -------------------------------------------------------------------------------------------------------------
def test_no_body_signature_or_token_reaches_the_logs(client, db, kyc_queue, caplog):
    caplog.set_level(logging.DEBUG)
    tag = "SENTINEL-BODY-" + uuid.uuid4().hex
    payload = webhook_payload("sdk_session_a")
    payload["data"]["verification_response"] = {"first_name": tag}
    body, headers = signed_webhook(payload, token="tok-" + tag[-12:])
    bad = {**headers, "x-prembly-signature": sign(body, "wrong-key")}
    assert client.post(URL, content=body, headers=bad).status_code == 401  # rejected path
    assert client.post(URL, content=b"{" + tag.encode(), headers={"x-prembly-signature": sign(b"{" + tag.encode(), TEST_WEBHOOK_KEY),
                                                                  "token": "t"}).status_code == 400
    kyc_queue.fail = RuntimeError(tag)  # a queue error that happens to carry text must not be echoed either
    assert client.post(URL, content=body, headers=headers).status_code == 500
    kyc_queue.fail = None
    assert client.post(URL, content=body, headers=headers).status_code == 200  # accepted path
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    for secret in (tag, headers["x-prembly-signature"], bad["x-prembly-signature"], headers["token"], TEST_WEBHOOK_KEY):
        assert secret not in logged
