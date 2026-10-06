"""W1: the Meta webhook. Signature over the raw body, dedupe, queueing, status callbacks."""

import json

import pytest

from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation, WhatsAppMessage
from app.tests.factories_whatsapp import make_contact, make_wa_id
from app.tests.fake_meta import (
    TEST_APP_SECRET, TEST_VERIFY_TOKEN, body_bytes, button_payload, fake_meta, inbound_queue,  # noqa: F401
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
