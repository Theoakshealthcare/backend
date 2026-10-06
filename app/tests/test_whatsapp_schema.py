"""W1: the schema rules (phone-first users, unique identities) and the worker job wiring."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models.consent import Consent
from app.db.models.support import SupportConversation
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation, WhatsAppMessage
from app.tests.factories import make_user
from app.tests.factories_whatsapp import make_contact, make_wa_id
from app.tests.fake_meta import fake_meta, text_payload  # noqa: F401  (fixture)


def test_users_can_exist_without_an_email_but_not_two_with_the_same_phone(db):
    a = make_user(db, email=None, phone_number="+2348011111111")
    assert a.email is None
    with pytest.raises(IntegrityError):
        make_user(db, email=None, phone_number="+2348011111111")
    db.rollback()


def test_several_users_without_an_email_can_coexist(db):
    make_user(db, email=None, phone_number="+2348011111112")
    make_user(db, email=None, phone_number="+2348011111113")
    assert db.query(User).filter(User.email.is_(None)).count() == 2


def test_app_registration_still_requires_an_email(client, sent_emails):
    r = client.post("/api/v1/auth/register", json={"phone_number": "+2348022222222", "full_name": "No Email",
                                                   "user_type": "patient", "password": "Passw0rd!test"})
    assert r.status_code == 422
    r = client.post("/api/v1/auth/register", json={"email": None, "full_name": "No Email",
                                                   "user_type": "patient", "password": "Passw0rd!test"})
    assert r.status_code == 422


def test_a_whatsapp_created_user_cannot_log_in_with_a_guessed_or_empty_password(client, db, fake_meta):
    from app.channels.whatsapp.ingest import ingest, parse_webhook
    from app.channels.whatsapp.processor import process_inbound

    wa_id = make_wa_id()
    for job in ingest(db, parse_webhook(text_payload(wa_id, "Hi"))).jobs:
        process_inbound(db, job)
    for password in ("", "password", "Passw0rd!test"):
        r = client.post("/api/v1/auth/login", data={"username": "+" + wa_id, "password": password})
        assert r.status_code in (401, 422), password


def test_contact_wa_id_and_phone_are_unique_and_e164_shaped(db):
    wa_id = make_wa_id()
    make_contact(db, wa_id)
    with pytest.raises(IntegrityError):
        make_contact(db, wa_id)
    db.rollback()
    with pytest.raises(IntegrityError):
        db.add(WhatsAppContact(wa_id="1", phone_e164="08012345678"))  # not E.164
        db.commit()
    db.rollback()


def test_one_conversation_per_contact(db):
    contact = make_contact(db)
    db.add(WhatsAppConversation(contact_id=contact.id, state="idle", context={}))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_conversation_state_is_constrained(db):
    contact = make_contact(db)
    with pytest.raises(IntegrityError):
        db.query(WhatsAppConversation).filter_by(contact_id=contact.id).update({"state": "bogus"})
    db.rollback()


def test_meta_message_id_is_unique_and_failed_sends_may_have_none(db):
    contact = make_contact(db)
    db.add_all([WhatsAppMessage(contact_id=contact.id, direction="outbound", type="text", status="failed"),
                WhatsAppMessage(contact_id=contact.id, direction="outbound", type="text", status="failed")])
    db.commit()  # two NULL ids are fine
    db.add(WhatsAppMessage(contact_id=contact.id, direction="inbound", meta_message_id="wamid.U", type="text",
                           status="received"))
    db.commit()
    db.add(WhatsAppMessage(contact_id=contact.id, direction="inbound", meta_message_id="wamid.U", type="text",
                           status="received"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_consents_are_append_only_in_the_database(db):
    contact = make_contact(db)
    row = Consent(contact_id=contact.id, subject_user_id=None, type="terms", decision="granted",
                  policy_version="v1", channel="whatsapp_button")
    db.add(row)
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE consents SET policy_version = 'v2'"))
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM consents"))
    db.rollback()
    row.policy_version = "v2"
    with pytest.raises(RuntimeError, match="append-only"):  # and the ORM refuses first
        db.commit()
    db.rollback()


def test_a_consent_needs_a_subject(db):
    db.add(Consent(type="terms", decision="granted", policy_version="v1", channel="whatsapp_button"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_support_conversation_constraints(db):
    contact = make_contact(db)
    db.add(SupportConversation(contact_id=contact.id, reason="emergency", priority="urgent", status="open"))
    db.commit()
    db.add(SupportConversation(contact_id=contact.id, reason="emergency", priority="shouting", status="open"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_only_one_open_ticket_per_contact(db):
    contact = make_contact(db)
    db.add(SupportConversation(contact_id=contact.id, reason="talk_to_person", priority="normal", status="open"))
    db.commit()
    db.add(SupportConversation(contact_id=contact.id, reason="two_misses", priority="normal", status="open"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_data_withdrawal_task_does_not_block_a_handover_ticket(db):
    contact = make_contact(db)
    db.add(SupportConversation(contact_id=contact.id, reason="data_withdrawal", priority="normal", status="open"))
    db.add(SupportConversation(contact_id=contact.id, reason="emergency", priority="urgent", status="open"))
    db.commit()
    assert db.query(SupportConversation).count() == 2


# --- worker wiring ---------------------------------------------------------------------------
def test_worker_registers_the_inbound_job():
    from app import worker

    names = {f.__name__ for f in worker.WorkerSettings.functions}
    assert "process_whatsapp_inbound_job" in names
    assert worker.WorkerSettings.max_tries >= 3


def test_the_job_runs_the_processor_with_a_fresh_session(db, fake_meta):
    from app import worker
    from app.channels.whatsapp.ingest import ingest, parse_webhook

    wa_id = make_wa_id()
    jobs = ingest(db, parse_webhook(text_payload(wa_id, "Hi", message_id="wamid.JOB1"))).jobs
    asyncio.run(worker.process_whatsapp_inbound_job({"job_try": 1}, jobs[0]))
    db.expire_all()
    assert db.query(WhatsAppConversation).one().state == "consent_pending"
    assert len(fake_meta.bodies) == 1


def test_a_failing_job_is_retried_with_backoff_until_it_runs_out_of_tries(db, fake_meta, monkeypatch):
    from arq import Retry

    from app import worker

    def boom(db, job, client=None):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(worker, "process_inbound", boom)
    with pytest.raises(Retry) as retry:
        asyncio.run(worker.process_whatsapp_inbound_job({"job_try": 1}, {"meta_message_id": "wamid.X"}))
    assert retry.value.defer_score and retry.value.defer_score > 0
    with pytest.raises(RuntimeError):  # last try: surface the failure (it lands in ARQ's failed results)
        asyncio.run(worker.process_whatsapp_inbound_job({"job_try": worker.WorkerSettings.max_tries},
                                                        {"meta_message_id": "wamid.X"}))


def test_the_job_payload_survives_the_json_serializer(db):
    from app import worker
    from app.channels.whatsapp.ingest import ingest, parse_webhook

    jobs = ingest(db, parse_webhook(text_payload(make_wa_id(), "Hi", message_id="wamid.JSON"))).jobs
    assert worker.json_deserializer(worker.json_serializer(jobs[0])) == jobs[0]


def test_resolve_endpoint_is_not_exposed_in_w1(client):
    """Agent actions arrive with the Admin console inbox; nothing in W1 lets a caller resolve tickets."""
    assert client.post("/api/v1/whatsapp/support/resolve").status_code in (404, 405)
