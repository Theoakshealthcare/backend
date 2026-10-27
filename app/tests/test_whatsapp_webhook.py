"""W1: the Meta webhook. Signature over the raw body, dedupe, queueing, status callbacks."""

import json

import pytest

from app.channels.whatsapp.ingest import parse_webhook
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation, WhatsAppMessage
from app.tests.factories_whatsapp import make_contact, make_wa_id
from app.tests.fake_meta import (
    TEST_APP_SECRET, TEST_VERIFY_TOKEN, body_bytes, bsuid, button_payload, fake_meta, inbound_queue,  # noqa: F401
    media_payload,
    sign, signed_headers, status_payload, text_payload,
)

URL = "/api/v1/whatsapp/webhook"


def post(client, payload=None, *, body=None, headers=None):
    body = body if body is not None else body_bytes(payload)
    return client.post(URL, content=body, headers=signed_headers(body) if headers is None else headers)


# --- GET verification handshake --------------------------------------------------------------
def test_verification_handshake_echoes_the_challenge(client, fake_meta):
    r = client.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": TEST_VERIFY_TOKEN,
                                "hub.challenge": "1158201444"})
    assert r.status_code == 200 and r.text == "1158201444"


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    {"hub.mode": "unsubscribe", "hub.verify_token": TEST_VERIFY_TOKEN, "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.challenge": "1"},
    {},
])
def test_verification_rejects_a_bad_token_or_mode(client, fake_meta, params):
    assert client.get(URL, params=params).status_code == 403


def test_verification_is_refused_when_no_verify_token_is_configured(client, fake_meta, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "")
    r = client.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"})
    assert r.status_code == 403


# --- POST signature --------------------------------------------------------------------------
def test_valid_signature_is_accepted_and_the_message_is_queued(client, db, fake_meta, inbound_queue):
    wa_id = make_wa_id()
    r = post(client, text_payload(wa_id, "Hi", message_id="wamid.A1"))
    assert r.status_code == 200
    assert [j["meta_message_id"] for j in inbound_queue.jobs] == ["wamid.A1"]
    row = db.query(WhatsAppMessage).one()
    assert (row.direction, row.type, row.status) == ("inbound", "text", "received")
    assert db.query(WhatsAppContact).filter_by(wa_id=wa_id).one().last_inbound_at is not None
    assert db.query(WhatsAppConversation).one().state == "new"


def test_invalid_signature_is_rejected_and_nothing_is_stored_or_queued(client, db, fake_meta, inbound_queue):
    body = body_bytes(text_payload(make_wa_id(), "Hi"))
    r = client.post(URL, content=body, headers={"X-Hub-Signature-256": sign(body, "some-other-secret")})
    assert r.status_code == 403
    assert not inbound_queue.jobs and db.query(WhatsAppMessage).count() == 0
    assert db.query(WhatsAppContact).count() == 0


def test_missing_signature_header_is_rejected(client, db, fake_meta, inbound_queue):
    r = client.post(URL, content=body_bytes(text_payload(make_wa_id(), "Hi")), headers={})
    assert r.status_code == 403 and not inbound_queue.jobs


@pytest.mark.parametrize("header", ["", "sha256=", "sha1=abcd", "abcd", "sha256=zz", "sha256=" + "0" * 64])
def test_malformed_signature_headers_are_rejected(client, fake_meta, inbound_queue, header):
    body = body_bytes(text_payload(make_wa_id(), "Hi"))
    r = client.post(URL, content=body, headers={"X-Hub-Signature-256": header})
    assert r.status_code == 403 and not inbound_queue.jobs


def test_empty_app_secret_rejects_everything_even_a_signature_made_with_the_empty_key(
        client, db, fake_meta, inbound_queue, monkeypatch):
    """An empty secret makes HMACs forgeable by anyone, so the endpoint must refuse rather than verify."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    body = body_bytes(text_payload(make_wa_id(), "Hi"))
    r = client.post(URL, content=body, headers={"X-Hub-Signature-256": sign(body, "")})
    assert r.status_code == 403 and not inbound_queue.jobs and db.query(WhatsAppMessage).count() == 0


def test_the_signature_covers_the_raw_bytes_so_tampering_is_caught(client, fake_meta, inbound_queue):
    payload = text_payload(make_wa_id(), "Hi", message_id="wamid.T1")
    body = body_bytes(payload)
    headers = signed_headers(body)
    tampered = body.replace(b'"Hi"', b'"Ho"')
    assert tampered != body
    assert client.post(URL, content=tampered, headers=headers).status_code == 403
    # re-serialising the same JSON with different whitespace is also a different byte string
    pretty = json.dumps(payload, indent=2).encode()
    assert client.post(URL, content=pretty, headers=headers).status_code == 403
    assert not inbound_queue.jobs


def test_a_signed_body_that_is_not_json_is_a_400(client, fake_meta, inbound_queue):
    body = b"not json"
    assert client.post(URL, content=body, headers=signed_headers(body)).status_code == 400


def test_oversized_bodies_are_refused_before_hashing(client, fake_meta, inbound_queue):
    body = b"x" * 1_500_000
    assert client.post(URL, content=body, headers=signed_headers(body)).status_code == 413


def test_a_payload_for_another_phone_number_id_is_ignored(client, db, fake_meta, inbound_queue):
    r = post(client, text_payload(make_wa_id(), "Hi", phone_number_id="999"))
    assert r.status_code == 200 and not inbound_queue.jobs and db.query(WhatsAppMessage).count() == 0


# --- dedupe / replay -------------------------------------------------------------------------
def test_a_replayed_message_is_acknowledged_but_queued_once(client, db, fake_meta, inbound_queue):
    payload = text_payload(make_wa_id(), "Hi", message_id="wamid.DUP")
    assert post(client, payload).status_code == 200
    assert post(client, payload).status_code == 200
    assert post(client, payload).status_code == 200
    assert len(inbound_queue.jobs) == 1
    assert db.query(WhatsAppMessage).count() == 1


def test_two_different_messages_are_both_queued(client, fake_meta, inbound_queue):
    wa_id = make_wa_id()
    post(client, text_payload(wa_id, "Hi", message_id="wamid.M1"))
    post(client, text_payload(wa_id, "Hello", message_id="wamid.M2"))
    assert [j["meta_message_id"] for j in inbound_queue.jobs] == ["wamid.M1", "wamid.M2"]


def test_one_contact_row_per_wa_id(client, db, fake_meta, inbound_queue):
    wa_id = make_wa_id()
    post(client, text_payload(wa_id, "Hi"))
    post(client, text_payload(wa_id, "Hi again"))
    assert db.query(WhatsAppContact).filter_by(wa_id=wa_id).count() == 1


def test_if_queueing_fails_the_message_is_forgotten_so_meta_can_retry(client, db, fake_meta, inbound_queue):
    inbound_queue.fail = ConnectionError("redis down")
    payload = text_payload(make_wa_id(), "Hi", message_id="wamid.RETRY")
    assert post(client, payload).status_code == 503
    assert db.query(WhatsAppMessage).count() == 0  # not deduped away: Meta's retry must be processed
    inbound_queue.fail = None
    assert post(client, payload).status_code == 200
    assert [j["meta_message_id"] for j in inbound_queue.jobs] == ["wamid.RETRY"]


def test_job_payload_carries_what_the_engine_needs_and_no_secrets(client, fake_meta, inbound_queue):
    post(client, button_payload(make_wa_id(), "consent:accept", "I agree", message_id="wamid.B1"))
    job = inbound_queue.jobs[0]
    assert job["type"] == "interactive" and job["reply_id"] == "consent:accept"
    assert TEST_APP_SECRET not in json.dumps(job)


# --- source attribution ----------------------------------------------------------------------
@pytest.mark.parametrize("kwargs,expected", [
    ({"text": "Hi"}, "direct"),
    ({"text": "Hi ref:LEKKI24 please"}, "ref:LEKKI24"),
    ({"text": "Hi", "referral": {"source_type": "ad", "source_id": "123"}}, "ctwa"),
])
def test_first_contact_source_is_recorded(client, db, fake_meta, inbound_queue, kwargs, expected):
    wa_id = make_wa_id()
    post(client, text_payload(wa_id, **kwargs))
    assert db.query(WhatsAppContact).filter_by(wa_id=wa_id).one().source == expected


# --- status callbacks ------------------------------------------------------------------------
def outbound_row(db, contact, meta_id="wamid.OUT1", status="sent"):
    db.add(WhatsAppMessage(contact_id=contact.id, direction="outbound", meta_message_id=meta_id, type="text",
                           status=status))
    db.commit()


def test_status_callbacks_advance_sent_delivered_read(client, db, fake_meta, inbound_queue):
    contact = make_contact(db)
    outbound_row(db, contact)
    for status in ("delivered", "read"):
        assert post(client, status_payload(contact.wa_id, "wamid.OUT1", status, category="utility")
                    ).status_code == 200
        db.expire_all()
        assert db.query(WhatsAppMessage).one().status == status
    assert db.query(WhatsAppMessage).one().cost_category == "utility"
    assert not inbound_queue.jobs  # statuses are never queued as conversation input


def test_out_of_order_callbacks_never_move_a_status_backwards(client, db, fake_meta):
    contact = make_contact(db)
    outbound_row(db, contact)
    post(client, status_payload(contact.wa_id, "wamid.OUT1", "read"))
    post(client, status_payload(contact.wa_id, "wamid.OUT1", "delivered"))
    post(client, status_payload(contact.wa_id, "wamid.OUT1", "sent"))
    db.expire_all()
    assert db.query(WhatsAppMessage).one().status == "read"


def test_failed_callback_records_the_error_code(client, db, fake_meta):
    contact = make_contact(db)
    outbound_row(db, contact)
    errors = [{"code": 131047, "title": "Re-engagement message"}]
    post(client, status_payload(contact.wa_id, "wamid.OUT1", "failed", errors=errors))
    db.expire_all()
    row = db.query(WhatsAppMessage).one()
    assert (row.status, row.error_code) == ("failed", "131047")


def test_a_status_for_an_unknown_contact_is_ignored(client, db, fake_meta):
    assert post(client, status_payload(make_wa_id(), "wamid.NOPE", "delivered")).status_code == 200
    assert db.query(WhatsAppMessage).count() == 0


def test_a_status_replay_is_harmless(client, db, fake_meta):
    contact = make_contact(db)
    outbound_row(db, contact)
    payload = status_payload(contact.wa_id, "wamid.OUT1", "delivered")
    for _ in range(3):
        assert post(client, payload).status_code == 200
    assert db.query(WhatsAppMessage).count() == 1


def test_a_batch_with_a_message_and_a_status_handles_both(client, db, fake_meta, inbound_queue):
    contact = make_contact(db)
    outbound_row(db, contact)
    payload = text_payload(contact.wa_id, "Hi", message_id="wamid.MIX")
    payload["entry"][0]["changes"][0]["value"]["statuses"] = status_payload(
        contact.wa_id, "wamid.OUT1", "delivered")["entry"][0]["changes"][0]["value"]["statuses"]
    assert post(client, payload).status_code == 200
    assert [j["meta_message_id"] for j in inbound_queue.jobs] == ["wamid.MIX"]
    db.expire_all()
    assert db.query(WhatsAppMessage).filter_by(meta_message_id="wamid.OUT1").one().status == "delivered"


# --- business-scoped user ids (BSUIDs) ------------------------------------------------------
def test_business_scoped_user_ids_are_accepted_and_ignored(client, db, fake_meta, inbound_queue):
    """Real Meta payloads carry ``user_id`` / ``from_user_id`` / ``recipient_user_id`` (``NG.<digits>``) next
    to every phone number. We do not store them yet (follow-up): the webhook must accept the payload,
    handle the message and the status as usual, and persist or queue none of the ids."""
    wa_id = make_wa_id()
    id_ = bsuid(wa_id)
    assert id_.startswith("NG.") and id_[3:].isdigit()
    message = text_payload(wa_id, "Hi", message_id="wamid.B1")
    value = message["entry"][0]["changes"][0]["value"]
    assert value["contacts"][0]["user_id"] == id_ and value["messages"][0]["from_user_id"] == id_

    assert post(client, message).status_code == 200
    status = status_payload(wa_id, "wamid.B1", "delivered")
    assert status["entry"][0]["changes"][0]["value"]["statuses"][0]["recipient_user_id"] == id_
    assert post(client, status).status_code == 200
    parsed = parse_webhook(status, expected_phone_number_id="")
    assert [(u.meta_message_id, u.status) for u in parsed.statuses] == [("wamid.B1", "delivered")]

    assert [j["meta_message_id"] for j in inbound_queue.jobs] == ["wamid.B1"]
    assert id_ not in json.dumps(inbound_queue.jobs, default=str)
    row = db.query(WhatsAppMessage).one()
    assert (row.type, row.status) == ("text", "received")  # a callback never rewrites an inbound row
    stored = [str(v) for r in (db.query(WhatsAppContact).all() + db.query(WhatsAppConversation).all()
                               + db.query(WhatsAppMessage).all()) for v in vars(r).values()]
    assert not any(id_ in v for v in stored)


# --- review fixes ----------------------------------------------------------------------------
def test_a_chunked_body_over_the_limit_is_refused_before_the_signature_is_checked(
        client, fake_meta, inbound_queue, monkeypatch):
    from app.api.v1.endpoints import whatsapp as endpoint

    checked = []
    monkeypatch.setattr(endpoint, "verify_signature", lambda *a, **k: checked.append(1) or True)
    chunk = b"x" * 600_000

    def chunks():  # a generator body goes out as chunked transfer: there is no Content-Length to check
        for _ in range(4):
            yield chunk

    r = client.post(URL, content=chunks(), headers={"x-hub-signature-256": "sha256=" + "0" * 64})
    assert r.status_code == 413 and not checked and not inbound_queue.jobs


def test_a_chunked_body_under_the_limit_is_still_accepted(client, db, fake_meta, inbound_queue):
    body = body_bytes(text_payload(make_wa_id(), "Hi", message_id="wamid.CHUNK"))

    def chunks():
        yield body[:50]
        yield body[50:]

    r = client.post(URL, content=chunks(), headers=signed_headers(body))
    assert r.status_code == 200 and [j["meta_message_id"] for j in inbound_queue.jobs] == ["wamid.CHUNK"]


def _raw_entry(message_or_value):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages",
            "value": message_or_value}]}]}


HOSTILE = [
    {"entry": "x"}, {"entry": [None, 3, "s", []]}, {"entry": [{"changes": "x"}]},
    {"entry": [{"changes": [None, 1, "x", {"field": "messages", "value": "oops"}]}]},
    {"entry": [{"changes": [{"field": "messages", "value": {"metadata": "x", "contacts": "x", "messages": "x"}}]}]},
    {"entry": [{"changes": [{"field": "messages", "value": {"contacts": [1, None, {"profile": "x"}],
                                                            "messages": [None, 4, "x", [], {"id": 1}]}}]}]},
    {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [None, "x", 3, {"id": []}]}}]}]},
]


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_but_validly_signed_payloads_are_a_200_not_a_500(client, db, fake_meta, inbound_queue, payload):
    assert post(client, payload).status_code == 200 and not inbound_queue.jobs


@pytest.mark.parametrize("message", [
    {"type": "text", "text": "not an object"}, {"type": "text", "text": {"body": 12345}},
    {"type": "text", "text": {"body": ["a"]}}, {"type": "text"},
    {"type": "interactive", "interactive": "x"}, {"type": "interactive", "interactive": {"type": ["x"]}},
    {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": "x"}},
    {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": {"a": 1}}}},
    {"type": "image", "image": "x"}, {"type": "image", "image": {"caption": 7}},
    {"type": "text", "id": "wamid.NUL\u0000", "text": {"body": "x"}}, {"type": "te\u0000xt", "text": {"body": "x"}},
])
def test_a_malformed_message_never_breaks_the_rest_of_the_batch(client, db, fake_meta, inbound_queue, message):
    wa_id = make_wa_id()
    good = text_payload(wa_id, "Hi", message_id="wamid.GOOD")["entry"][0]["changes"][0]["value"]
    bad = {"from": wa_id, "id": "wamid.BAD", "timestamp": "1", **message}
    good["messages"].insert(0, bad)
    r = post(client, _raw_entry(good))
    assert r.status_code == 200
    ids = [j["meta_message_id"] for j in inbound_queue.jobs]
    assert "wamid.GOOD" in ids
    for job in inbound_queue.jobs:
        assert job["text"] is None or isinstance(job["text"], str)
        assert job["reply_id"] is None or isinstance(job["reply_id"], str)


def test_an_image_caption_travels_with_the_job(client, fake_meta, inbound_queue):
    payload = media_payload(make_wa_id(), "image", message_id="wamid.IMG")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = "she cannot breathe"
    assert post(client, payload).status_code == 200
    job = inbound_queue.jobs[0]
    assert job["type"] == "image" and job["text"] == "she cannot breathe"


@pytest.mark.parametrize("kind", ["video", "document"])
def test_video_and_document_captions_are_read_too(client, fake_meta, inbound_queue, kind):
    payload = media_payload(make_wa_id(), kind, message_id=f"wamid.{kind}")
    payload["entry"][0]["changes"][0]["value"]["messages"][0][kind]["caption"] = "not breathing"
    post(client, payload)
    assert inbound_queue.jobs[0]["text"] == "not breathing"


def test_a_none_from_the_queue_is_treated_as_failure_and_retry_is_processed_exactly_once(
        client, db, fake_meta, inbound_queue):
    """enqueue_job writes then raises (or reports the id as taken): the row is forgotten, Meta's retry works."""
    inbound_queue.fail_after_write = ConnectionError("reset after write")
    payload = text_payload(make_wa_id(), "Hi", message_id="wamid.AFTERWRITE")
    assert post(client, payload).status_code == 503
    assert db.query(WhatsAppMessage).count() == 0
    inbound_queue.fail_after_write = None
    assert post(client, payload).status_code == 200
    assert post(client, payload).status_code == 200  # a further replay is deduped by the database
    assert [j["meta_message_id"] for j in inbound_queue.jobs] == ["wamid.AFTERWRITE"]  # once, not twice
    assert db.query(WhatsAppMessage).count() == 1


def test_the_body_reader_stops_consuming_the_stream_at_the_cap():
    import asyncio

    from fastapi import HTTPException

    from app.api.v1.endpoints.whatsapp import MAX_BODY_BYTES, _read_capped_body

    consumed = []

    class FakeRequest:
        headers: dict = {}

        async def stream(self):  # an endless chunked body
            while True:
                consumed.append(1)
                yield b"x" * (MAX_BODY_BYTES // 2 + 1)

        async def body(self):  # the old code path: must never be used
            raise AssertionError("request.body() buffers the whole stream")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_read_capped_body(FakeRequest()))
    assert excinfo.value.status_code == 413 and len(consumed) == 2
