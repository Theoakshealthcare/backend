"""KD1: the public Dojah webhook. Signature over the raw body, the derived dedupe key, enqueue compensation, the body cap.

Design: docs/plans/2026-10-27-caregiver-kyc-design.md section 8.1 and 16.1; docs/plans/2026-10-30-caregiver-kyc-dojah-delta.md. Dojah
documents no idempotency header, so the ledger key is derived from reference + status word + body hash (kyc_seam D2). Every key and
reference here is a generated placeholder.
"""
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app.core.config import settings
from app.db.models.identity_verification import KycWebhookEvent
from app.services import kyc_seam as seam
from app.tests.fake_dojah import (
    SENTINEL_PHOTO, SENTINEL_URLS, TEST_WEBHOOK_SECRET, FakeDojah, body_bytes, fake_dojah, kyc_queue, new_reference, sign,  # noqa: F401
    signed_webhook, webhook_payload,
)

URL = "/api/v1/kyc/dojah/webhook"
SENTINEL = "SENTINEL-REGISTRY-RECORD-do-not-store"
REF = new_reference()


@pytest.fixture(autouse=True)
def env(fake_dojah, kyc_queue):  # noqa: F811
    return kyc_queue


def post(client, payload=None, *, secret=TEST_WEBHOOK_SECRET):
    body, headers = signed_webhook(payload if payload is not None else webhook_payload(REF), secret)
    return client.post(URL, content=body, headers=headers)


def rows(db):
    db.expire_all()
    return db.query(KycWebhookEvent).all()


# --- the happy path ---------------------------------------------------------------------------------------------------
def test_a_signed_event_is_recorded_and_queued(client, db, kyc_queue):
    r = post(client, webhook_payload(REF))
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    (row,) = rows(db)
    assert (row.provider_session_id, row.event_type, row.signature_ok) == (REF, "completed", True)
    assert len(row.token) == 64 and len(row.body_sha256) == 64  # the derived dedupe key and the body hash
    assert row.processed_at is None and row.outcome is None and row.verification_id is None
    # opaque facts only: not even the reference rides through Redis (the worker reads it from the ledger row)
    assert kyc_queue.jobs == [{"event_id": str(row.id), "claim": None, "settled": True}]


def test_the_route_is_public_and_hidden_from_the_schema(client):
    assert URL.removeprefix("/api/v1") not in client.get("/api/v1/openapi.json").json()["paths"]


def test_the_old_provider_route_is_gone(client):
    body, headers = signed_webhook(webhook_payload(REF))
    assert client.post("/api/v1/kyc/prembly/webhook", content=body, headers=headers).status_code == 404


def test_the_body_the_photo_and_the_image_links_are_not_stored_anywhere(client, db, kyc_queue):
    entity = FakeDojah.entity(REF)
    payload = webhook_payload(REF, entity=entity, extra_note=SENTINEL)
    payload["data"]["government_data"]["data"]["nin"]["entity"]["first_name"] = SENTINEL
    assert post(client, payload).status_code == 200
    stored = json.dumps([{c.name: str(getattr(r, c.name)) for c in r.__table__.columns} for r in rows(db)]) + json.dumps(kyc_queue.jobs)
    for needle in (SENTINEL, SENTINEL_PHOTO, *SENTINEL_URLS.values()):
        assert needle not in stored


@pytest.mark.parametrize("where", ["reference_id", "referenceId", "data.reference_id"])
def test_the_reference_is_read_from_the_top_level_and_the_defensive_spellings(client, db, where):
    payload = {"verification_status": "Completed"}
    if where == "data.reference_id":
        payload["data"] = {"reference_id": REF}
    else:
        payload[where] = REF
    assert post(client, payload).status_code == 200
    assert rows(db)[0].provider_session_id == REF


@pytest.mark.parametrize("status, claim, settled, stored", [
    ("Completed", None, True, "completed"), ("Failed", "fail", True, "failed"), ("Pending", None, False, "pending"),
    ("Ongoing", None, False, "ongoing"), ("Abandoned", None, False, "abandoned"), ("SomethingNew", None, False, None),
    (None, None, False, None), (7, None, False, None),
])
def test_the_status_word_sets_the_claim_and_the_settled_flag(client, db, kyc_queue, status, claim, settled, stored):
    payload = webhook_payload(REF)
    payload["verification_status"] = status
    assert post(client, payload).status_code == 200
    assert rows(db)[0].event_type == stored
    assert (kyc_queue.jobs[0]["claim"], kyc_queue.jobs[0]["settled"]) == (claim, settled)


def test_a_completed_webhook_never_claims_a_pass(client, kyc_queue):
    """Completed means the flow ended, not that the person passed: the webhook can only ever claim a failure."""
    assert post(client, webhook_payload(REF, entity=FakeDojah.entity(REF))).status_code == 200
    assert kyc_queue.jobs[0]["claim"] is None


def test_a_signed_event_for_a_reference_that_is_not_ours_is_acknowledged_and_not_recorded(client, db, kyc_queue):
    for foreign in ("KYC-BVN-1234", "okl-" + uuid.uuid4().hex, "OKL-" + uuid.uuid4().hex[:31], "OKL-" + uuid.uuid4().hex.upper()):
        assert post(client, webhook_payload(foreign)).status_code == 200
    assert rows(db) == [] and not kyc_queue.jobs


# --- signature --------------------------------------------------------------------------------------------------------
def test_a_bad_signature_is_refused_and_nothing_is_stored_or_queued(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload(REF), "some-other-secret")
    r = client.post(URL, content=body, headers=headers)
    assert r.status_code == 401 and not kyc_queue.jobs and rows(db) == []


def test_a_missing_signature_is_refused(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload(REF))
    del headers["x-dojah-signature"]
    assert client.post(URL, content=body, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


def test_the_v2_signature_header_alone_does_not_verify(client, db, kyc_queue):
    """``x-dojah-signature-v2`` is a hash of the secret alone (it does not bind the payload): it is ignored."""
    body, headers = signed_webhook(webhook_payload(REF))
    headers = {"content-type": "application/json", "x-dojah-signature-v2": headers["x-dojah-signature"]}
    assert client.post(URL, content=body, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


@pytest.mark.parametrize("header", ["", " ", "sha256=abcd", "abcd", "zz==", "0" * 64, b"\xe9" * 64])
def test_malformed_signature_headers_are_refused(client, db, kyc_queue, header):
    body, headers = signed_webhook(webhook_payload(REF))
    headers["x-dojah-signature"] = header
    assert client.post(URL, content=body, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


def test_the_signature_is_hex_not_base64(client, db, kyc_queue):
    import base64
    import hashlib
    import hmac

    body, headers = signed_webhook(webhook_payload(REF))
    headers["x-dojah-signature"] = base64.b64encode(hmac.new(TEST_WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert client.post(URL, content=body, headers=headers).status_code == 401 and rows(db) == []


def test_an_upper_case_or_padded_hex_signature_verifies(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload(REF))
    headers["x-dojah-signature"] = f"  {headers['x-dojah-signature'].upper()}\n"
    assert client.post(URL, content=body, headers=headers).status_code == 200 and len(rows(db)) == 1


def test_a_tampered_body_is_refused(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload(REF))
    tampered = body.replace(b'"Completed"', b'"Failed"')
    assert tampered != body
    assert client.post(URL, content=tampered, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


def test_a_re_serialised_body_does_not_verify(client, kyc_queue):
    """The signature is over the exact bytes: the same JSON with different spacing is a different message."""
    payload = webhook_payload(REF)
    body, headers = signed_webhook(payload)
    respaced = json.dumps(payload).encode()  # default separators: valid JSON, different bytes
    assert respaced != body
    assert client.post(URL, content=respaced, headers=headers).status_code == 401 and not kyc_queue.jobs


def test_an_empty_secret_never_verifies_even_for_a_signature_made_with_it(client, db, kyc_queue, monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", "")
    body, headers = signed_webhook(webhook_payload(REF), secret="")  # anyone can compute this
    assert client.post(URL, content=body, headers=headers).status_code == 401
    body, headers = signed_webhook(webhook_payload(REF), secret=TEST_WEBHOOK_SECRET)
    assert client.post(URL, content=body, headers=headers).status_code == 401
    assert not kyc_queue.jobs and rows(db) == []


def test_the_api_secret_key_is_not_the_webhook_secret(client, db, kyc_queue):
    from app.tests.fake_dojah import TEST_SECRET_KEY

    assert post(client, secret=TEST_SECRET_KEY).status_code == 401 and rows(db) == []


def test_verify_signature_unit():
    body = b'{"a":1}'
    good = sign(body, "k")
    assert seam.verify_signature(body, good, "k") and seam.verify_signature(body, f" {good.upper()}\n", "k")
    assert not seam.verify_signature(body, good, "")
    assert not seam.verify_signature(body, good, "other")
    assert not seam.verify_signature(body, None, "k") and not seam.verify_signature(body, "", "k")
    assert not seam.verify_signature(body + b" ", good, "k")
    assert not seam.verify_signature(body, "éé", "k")  # non-ASCII text: refused, not an error
    assert not seam.verify_signature(body, sign(body, ""), "k")
    assert not seam.verify_signature(body, sign(body, ""), "")  # an empty secret verifies nothing, not even its own signature


# --- JSON and payload -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe", b"[]", b'"x"', b"null", b"{}", b'{"verification_status":"x"}',
                                 b'{"reference_id":""}', b'{"reference_id":7}', b'{"reference_id":"../../etc"}',
                                 b'{"reference_id":"a b"}', b'{"reference_id":"abc\\n"}', b'{"reference_id":"' + b"x" * 200 + b'"}',
                                 b"[" * 100_000])
def test_a_signed_body_that_is_not_an_event_is_a_400(client, db, kyc_queue, raw):
    headers = {"x-dojah-signature": sign(raw, TEST_WEBHOOK_SECRET)}
    assert client.post(URL, content=raw, headers=headers).status_code == 400
    assert not kyc_queue.jobs and rows(db) == []


# --- dedupe and replay ------------------------------------------------------------------------------------------------
def test_a_retry_of_the_same_body_is_acknowledged_once_and_queued_once(client, db, kyc_queue):
    payload = webhook_payload(REF)
    assert post(client, payload).status_code == 200
    kyc_queue.jobs.clear()
    from datetime import datetime as _dt

    db.query(KycWebhookEvent).update({"processed_at": _dt.utcnow(), "outcome": "applied"})
    db.commit()
    assert post(client, payload).status_code == 200  # Dojah retried; we already have it
    assert len(rows(db)) == 1 and not kyc_queue.jobs


def test_the_same_body_twice_before_it_is_processed_is_one_row_and_one_job(client, db, kyc_queue):
    payload = webhook_payload(REF)
    assert post(client, payload).status_code == 200 and post(client, payload).status_code == 200
    assert len(rows(db)) == 1 and len(kyc_queue.jobs) == 1


def test_the_same_reference_with_another_status_is_another_event(client, db, kyc_queue):
    assert post(client, webhook_payload(REF, status="Pending")).status_code == 200
    assert post(client, webhook_payload(REF, status="Completed")).status_code == 200
    assert len(rows(db)) == 2 and len(kyc_queue.jobs) == 2


def test_the_same_reference_and_status_with_another_body_is_another_event(client, db, kyc_queue):
    assert post(client, webhook_payload(REF, note="a")).status_code == 200
    assert post(client, webhook_payload(REF, note="b")).status_code == 200
    assert len(rows(db)) == 2


def test_the_dedupe_key_unit():
    event = seam.parse_event({"reference_id": REF, "verification_status": "Completed"})
    other = seam.parse_event({"reference_id": new_reference(), "verification_status": "Completed"})
    again = seam.parse_event({"reference_id": REF, "verification_status": "Failed"})
    assert seam.dedupe_key(b"x", event) == seam.dedupe_key(b"x", event)
    assert len({seam.dedupe_key(b"x", event), seam.dedupe_key(b"y", event), seam.dedupe_key(b"x", other),
                seam.dedupe_key(b"x", again)}) == 4
    assert REF not in seam.dedupe_key(b"x", event)


def test_a_replayed_body_after_it_was_processed_is_inert_at_the_transport(client, db, kyc_queue):
    """No timestamp exists to bound a replay (kyc_seam D2/D3). Transport dedupe is by the derived key; state-level inertness (a
    terminal attempt ignores events, a finished attempt costs no pull) is tested in test_kyc_events."""
    body, headers = signed_webhook(webhook_payload(REF))
    assert client.post(URL, content=body, headers=headers).status_code == 200
    db.query(KycWebhookEvent).update({"processed_at": datetime.utcnow(), "outcome": "applied"})
    db.commit()
    kyc_queue.jobs.clear()
    for _ in range(3):
        assert client.post(URL, content=body, headers=headers).status_code == 200
    assert len(rows(db)) == 1 and not kyc_queue.jobs


def test_a_redelivery_after_a_lost_job_is_queued_again(client, db, kyc_queue):
    """The first delivery died between the insert and the enqueue: its row exists, no job does. The retry must not be deduped."""
    payload = webhook_payload(REF)
    assert post(client, payload).status_code == 200
    kyc_queue.jobs.clear()  # the job never reached Redis
    assert post(client, payload).status_code == 200
    assert len(rows(db)) == 1 and [j["event_id"] for j in kyc_queue.jobs] == [str(rows(db)[0].id)]


def test_a_failed_requeue_of_a_redelivery_keeps_the_existing_row(client, db, kyc_queue):
    payload = webhook_payload(REF)
    assert post(client, payload).status_code == 200
    kyc_queue.jobs.clear()
    kyc_queue.fail = ConnectionError("redis down")
    assert post(client, payload).status_code == 500
    assert len(rows(db)) == 1  # not compensated: this request did not create it
    kyc_queue.fail = None
    assert post(client, payload).status_code == 200 and len(kyc_queue.jobs) == 1


def test_concurrent_deliveries_of_one_body_record_one_row(client, db, kyc_queue):
    body, headers = signed_webhook(webhook_payload(REF))
    with ThreadPoolExecutor(4) as pool:
        statuses = list(pool.map(lambda _: client.post(URL, content=body, headers=headers).status_code, range(4)))
    assert statuses == [200] * 4 and len(rows(db)) == 1 and len(kyc_queue.jobs) == 1


# --- enqueue failure ---------------------------------------------------------------------------------------------------
def test_an_enqueue_failure_undoes_the_row_and_asks_for_a_retry(client, db, kyc_queue):
    kyc_queue.fail = ConnectionError("redis down")
    r = post(client, webhook_payload(REF))
    assert r.status_code == seam.ENQUEUE_FAILURE_STATUS == 500
    assert rows(db) == [] and not kyc_queue.jobs

    kyc_queue.fail = None  # the retry, same body: processed, not deduped away
    assert post(client, webhook_payload(REF)).status_code == 200
    assert len(rows(db)) == 1 and len(kyc_queue.jobs) == 1


def test_a_refused_job_id_means_a_twin_delivery_already_queued_it_and_the_row_stays(client, db, kyc_queue):
    """The job id is the ledger row's id: a refusal can only mean another delivery of the same body got its job in first. That job
    needs the row, so it is neither forgotten nor answered with a retry request."""
    from app.services.kyc_queue import QueueRefused

    kyc_queue.fail = QueueRefused("already queued")
    assert post(client, webhook_payload(REF)).status_code == 200 and len(rows(db)) == 1


# --- size cap ---------------------------------------------------------------------------------------------------------
def test_an_oversized_body_is_refused_before_it_is_hashed(client, db, kyc_queue, monkeypatch):
    monkeypatch.setattr(seam, "MAX_BODY_BYTES", 1_000)
    big = b'{"reference_id":"s","pad":"' + b"x" * 2_000 + b'"}'
    headers = {"x-dojah-signature": sign(big, TEST_WEBHOOK_SECRET)}  # even a VALID signature
    assert client.post(URL, content=big, headers=headers).status_code == 413
    assert not kyc_queue.jobs and rows(db) == []


def test_a_chunked_oversized_body_is_cut_off(client, kyc_queue, monkeypatch):
    monkeypatch.setattr(seam, "MAX_BODY_BYTES", 1_000)

    def chunks():
        for _ in range(10):
            yield b"x" * 400

    assert client.post(URL, content=chunks(), headers={"x-dojah-signature": "x"}).status_code == 413


def test_a_body_at_the_cap_is_still_read(client, kyc_queue, monkeypatch):
    payload = webhook_payload(REF)
    monkeypatch.setattr(seam, "MAX_BODY_BYTES", len(body_bytes(payload)))
    assert post(client, payload).status_code == 200


# --- logs -------------------------------------------------------------------------------------------------------------
def test_no_body_signature_reference_or_image_link_reaches_the_logs(client, db, kyc_queue, caplog):
    caplog.set_level(logging.DEBUG)
    tag = "SENTINEL-BODY-" + uuid.uuid4().hex
    payload = webhook_payload(REF, entity=FakeDojah.entity(REF), note=tag)
    body, headers = signed_webhook(payload)
    bad = {**headers, "x-dojah-signature": sign(body, "wrong-secret")}
    assert client.post(URL, content=body, headers=bad).status_code == 401  # rejected path
    junk = b"{" + tag.encode()
    assert client.post(URL, content=junk, headers={"x-dojah-signature": sign(junk, TEST_WEBHOOK_SECRET)}).status_code == 400
    kyc_queue.fail = RuntimeError(tag)  # a queue error that happens to carry text must not be echoed either
    assert client.post(URL, content=body, headers=headers).status_code == 500
    kyc_queue.fail = None
    assert client.post(URL, content=body, headers=headers).status_code == 200  # accepted path
    foreign = webhook_payload("KYC-OTHER-" + tag[-12:])
    assert post(client, foreign).status_code == 200  # the ignored path
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    for secret in (tag, headers["x-dojah-signature"], bad["x-dojah-signature"], TEST_WEBHOOK_SECRET, REF, tag[-12:],
                   SENTINEL_PHOTO, *SENTINEL_URLS.values()):
        assert secret not in logged
