"""W1: inbound processing end to end against a real database and the fake Meta Graph API.

``chat(...)`` builds a Meta-shaped webhook, runs it through ``ingest`` (what the webhook does) and
``process_inbound`` (what the ARQ job does), then returns what the bot sent back.
"""

import json

import pytest
from sqlalchemy import text

from app.channels.whatsapp.ingest import ingest, parse_webhook
from app.channels.whatsapp.processor import process_inbound
from app.core.config import settings
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.consent import Consent
from app.db.models.patient import Patient
from app.db.models.support import SupportConversation
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation, WhatsAppMessage
from app.services.consent import ConsentService
from app.services.support import SupportService
from app.tests.factories import make_user
from app.tests.factories_whatsapp import make_wa_id
from app.tests.fake_meta import (  # noqa: F401  (fake_meta is a fixture)
    button_payload, fake_meta, list_payload, location_payload, media_payload, text_payload,
)


class Chat:
    def __init__(self, db, fake, wa_id=None):
        self.db, self.fake, self.wa_id = db, fake, wa_id or make_wa_id()

    def send(self, payload):
        before = len(self.fake.bodies)
        result = ingest(self.db, parse_webhook(payload))
        for job in result.jobs:
            process_inbound(self.db, job)
        return self.fake.bodies[before:]

    def say(self, text_, **kw):
        return self.send(text_payload(self.wa_id, text_, **kw))

    def tap(self, reply_id):
        return self.send(button_payload(self.wa_id, reply_id))

    def pick(self, reply_id):
        return self.send(list_payload(self.wa_id, reply_id))

    @property
    def contact(self):
        self.db.expire_all()
        return self.db.query(WhatsAppContact).filter_by(wa_id=self.wa_id).one()

    @property
    def conversation(self):
        self.db.expire_all()
        return self.db.query(WhatsAppConversation).filter_by(contact_id=self.contact.id).one()

    @property
    def state(self):
        return self.conversation.state


@pytest.fixture
def chat(db, fake_meta):
    return Chat(db, fake_meta)


def join(bodies):
    return " ".join(json.dumps(b) for b in bodies)


# --- first contact ---------------------------------------------------------------------------
def test_first_contact_creates_contact_phone_first_user_and_replies_with_consent(chat, db):
    sent = chat.say("Hi", name="Tolu A.")
    contact = chat.contact
    assert contact.phone_e164 == "+" + chat.wa_id and contact.display_name == "Tolu A."
    user = db.get(User, contact.user_id)
    assert (user.email, user.phone_number, user.user_type) == (None, "+" + chat.wa_id, "patient")
    assert user.hashed_password and user.is_active
    patient = db.query(Patient).filter_by(user_id=user.id).one()
    assert patient.email is None and patient.phone_number == "+" + chat.wa_id
    assert chat.state == "consent_pending"
    assert len(sent) == 1 and sent[0]["to"] == chat.wa_id
    assert sent[0]["interactive"]["type"] == "button"
    assert {b["reply"]["id"] for b in sent[0]["interactive"]["action"]["buttons"]} == {
        "consent:accept", "consent:decline"}


def test_first_contact_creates_a_self_care_recipient_and_relationship(chat, db):
    chat.say("Hi")
    user_id = chat.contact.user_id
    rel = db.query(CareRelationship).one()
    assert (rel.contact_user_id, rel.role, rel.clinical_access) == (user_id, "self", True)
    assert db.get(CareRecipient, rel.recipient_id).created_by_user_id == user_id


def test_a_second_message_does_not_create_a_second_user(chat, db):
    chat.say("Hi")
    chat.say("Hi")
    assert db.query(User).count() == 1 and db.query(WhatsAppContact).count() == 1


def test_a_phone_that_already_has_an_app_account_is_not_silently_linked(chat, db):
    """Linking needs a one-time code (WA-FR-3). Until that exists the WhatsApp user must not be handed
    someone else's account just because the numbers match."""
    existing = make_user(db, phone_number="0" + chat.wa_id[3:])  # the same number, stored nationally
    chat.say("Hi")
    contact = chat.contact
    assert contact.user_id is None
    assert chat.conversation.context.get("link_required") is True
    assert db.query(User).count() == 1 and db.get(User, existing.id).phone_number.startswith("0")
    assert chat.state == "consent_pending"  # the conversation still works: consent and the menu need no account


def test_consent_is_recorded_against_the_contact_even_without_a_user(chat, db):
    make_user(db, phone_number="+" + chat.wa_id)
    chat.say("Hi")
    chat.tap("consent:accept")
    assert {c.type for c in db.query(Consent).all()} == {"terms", "health_processing"}
    assert all(c.contact_id == chat.contact.id for c in db.query(Consent).all())


# --- consent ---------------------------------------------------------------------------------
def test_accepting_consent_stores_policy_version_and_the_message_id_as_evidence(chat, db):
    chat.say("Hi")
    sent = chat.tap("consent:accept")
    rows = {c.type: c for c in db.query(Consent).all()}
    assert set(rows) == {"terms", "health_processing"}
    for row in rows.values():
        assert (row.decision, row.channel, row.policy_version) == ("granted", "whatsapp_button",
                                                                    settings.WHATSAPP_POLICY_VERSION)
        assert row.evidence_ref.startswith("wamid.") and row.granted_by_user_id == chat.contact.user_id
        assert row.subject_user_id == chat.contact.user_id
    assert chat.contact.opted_in_at is not None and chat.contact.marketing_opt_in is False
    assert chat.state == "idle"
    assert sent[0]["interactive"]["type"] == "list"


def test_declining_consent_stores_nothing_and_stays_quiet_afterwards(chat, db):
    chat.say("Hi")
    sent = chat.tap("consent:decline")
    assert db.query(Consent).count() == 0 and chat.state == "declined"
    assert {b["reply"]["id"] for b in sent[0]["interactive"]["action"]["buttons"]} == {
        "declined:human", "consent:review"}
    again = chat.say("hello?")
    assert len(again) == 1 and chat.state == "declined"          # only the reply to that message
    assert db.query(Consent).count() == 0


def test_declined_user_can_review_and_accept_later(chat, db):
    chat.say("Hi")
    chat.tap("consent:decline")
    chat.tap("consent:review")
    chat.tap("consent:accept")
    assert chat.state == "idle" and db.query(Consent).count() == 2


def test_consent_state_is_derived_from_the_latest_event(chat, db):
    chat.say("Hi")
    chat.tap("consent:accept")
    service = ConsentService(db)
    contact = chat.contact
    assert service.has_required_consent(contact_id=contact.id)
    chat.say("STOP DATA")
    assert not service.has_required_consent(contact_id=contact.id)
    chat.say("start")
    chat.tap("consent:accept")
    assert service.has_required_consent(contact_id=contact.id)
    assert db.query(Consent).count() == 6  # granted x2, withdrawn x2 (marketing was never granted), granted x2


def test_a_replayed_consent_button_does_not_double_record(chat, db):
    chat.say("Hi")
    payload = button_payload(chat.wa_id, "consent:accept", message_id="wamid.SAME")
    chat.send(payload)
    chat.send(payload)
    assert db.query(Consent).count() == 2


def test_consents_cannot_be_updated_or_deleted(chat, db):
    from sqlalchemy.exc import DBAPIError

    chat.say("Hi")
    chat.tap("consent:accept")
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE consents SET decision = 'withdrawn'"))
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM consents"))
    db.rollback()


# --- STOP / STOP DATA / START ----------------------------------------------------------------
def onboarded(chat):
    chat.say("Hi")
    chat.tap("consent:accept")


def test_stop_opts_out_and_later_messages_get_only_the_hint(chat, db):
    onboarded(chat)
    sent = chat.say("STOP")
    assert len(sent) == 1 and chat.contact.opted_out_at is not None
    hint = chat.say("hello")
    assert len(hint) == 1 and "START" in hint[0]["text"]["body"]
    assert db.query(SupportConversation).count() == 0


def test_start_resubscribes(chat, db):
    onboarded(chat)
    chat.say("STOP")
    sent = chat.say("START")
    assert chat.contact.opted_out_at is None
    assert sent[0]["interactive"]["type"] == "list"


def test_stop_data_withdraws_consent_and_opens_a_compliance_ticket(chat, db):
    onboarded(chat)
    sent = chat.say("STOP DATA")
    withdrawn = db.query(Consent).filter_by(decision="withdrawn").all()
    assert {c.type for c in withdrawn} == {"terms", "health_processing"}  # marketing was never granted
    ticket = db.query(SupportConversation).one()
    assert (ticket.reason, ticket.priority, ticket.status) == ("data_withdrawal", "normal", "open")
    assert chat.state == "declined" and chat.contact.opted_out_at is not None
    assert "paid" in sent[0]["text"]["body"].lower()


def test_marketing_consent_is_withdrawn_too_when_it_was_granted(chat, db):
    onboarded(chat)
    ConsentService(db).record(contact_id=chat.contact.id, subject_user_id=chat.contact.user_id,
                              granted_by_user_id=chat.contact.user_id, types=["marketing"],
                              decision="granted", channel="whatsapp_button", evidence_ref="wamid.X")
    db.commit()
    chat.say("STOP DATA")
    assert {c.type for c in db.query(Consent).filter_by(decision="withdrawn")} == {
        "terms", "health_processing", "marketing"}


def test_start_after_stop_data_goes_back_through_consent(chat, db):
    onboarded(chat)
    chat.say("STOP DATA")
    sent = chat.say("START")
    assert chat.state == "consent_pending" and sent[0]["interactive"]["type"] == "button"


# --- menu and placeholders -------------------------------------------------------------------
def test_menu_options_route_to_a_human_while_booking_is_not_built(chat, db):
    onboarded(chat)
    sent = chat.pick("menu:book")
    assert chat.state == "human" and chat.conversation.automation_paused is True
    assert "coming soon" in sent[0]["text"]["body"].lower()
    ticket = db.query(SupportConversation).one()
    assert (ticket.reason, ticket.priority) == ("booking_placeholder", "normal")
    assert chat.say("hello?") == []  # a human owns it now: the bot is silent


def test_two_unknown_messages_in_a_row_hand_over_to_a_person(chat, db):
    onboarded(chat)
    first = chat.say("asdf")
    assert chat.state == "idle" and first[0]["interactive"]["type"] == "list"
    second = chat.say("qwer")
    assert chat.state == "human"
    ticket = db.query(SupportConversation).one()
    assert (ticket.reason, ticket.status) == ("two_misses", "open")
    assert len(second) == 1 and chat.say("anyone?") == []


def test_a_recognised_message_between_misses_resets_the_count(chat, db):
    onboarded(chat)
    chat.say("asdf")
    chat.say("menu")
    chat.say("qwer")
    assert chat.state == "idle" and db.query(SupportConversation).count() == 0


def test_non_text_inputs_count_as_misses_and_get_a_reply(chat, db):
    onboarded(chat)
    assert len(chat.send(media_payload(chat.wa_id, "image"))) == 1
    assert len(chat.send(location_payload(chat.wa_id))) == 1
    assert chat.state == "human"


# --- emergency -------------------------------------------------------------------------------
@pytest.mark.parametrize("message", [
    "my mother has chest pain", "he is not breathing", "I no fit breathe", "e don faint", "I wan kill myself",
])
def test_emergency_replies_at_once_opens_an_urgent_ticket_and_pauses_automation(chat, db, message):
    onboarded(chat)
    sent = chat.say(message)
    assert len(sent) == 1 and "112" in sent[0]["text"]["body"]
    ticket = db.query(SupportConversation).one()
    assert (ticket.reason, ticket.priority, ticket.status) == ("emergency", "urgent", "open")
    conv = chat.conversation
    assert conv.state == "emergency" and conv.automation_paused is True
    assert chat.say("hello?") == []                    # automation stays paused
    assert chat.pick("menu:book") == []


def test_emergency_works_before_consent_and_for_a_brand_new_contact(chat, db):
    sent = chat.say("please help she is not breathing")
    assert "112" in sent[0]["text"]["body"] and chat.state == "emergency"
    assert db.query(SupportConversation).one().priority == "urgent"


def test_emergency_reaches_an_opted_out_contact(chat, db):
    onboarded(chat)
    chat.say("STOP")
    sent = chat.say("chest pain")
    assert len(sent) == 1 and "112" in sent[0]["text"]["body"]


def test_repeating_an_emergency_resends_the_screen_but_does_not_duplicate_the_ticket(chat, db):
    onboarded(chat)
    chat.say("chest pain")
    sent = chat.say("chest pain please hurry")
    assert len(sent) == 1
    assert db.query(SupportConversation).count() == 1


def test_an_emergency_escalates_an_open_normal_ticket_to_urgent(chat, db):
    onboarded(chat)
    chat.pick("menu:human")
    assert db.query(SupportConversation).one().priority == "normal"
    chat.say("he is not breathing")
    ticket = db.query(SupportConversation).one()
    assert ticket.priority == "urgent" and ticket.reason == "emergency"


def test_resolving_the_ticket_resumes_automation(chat, db):
    onboarded(chat)
    chat.say("chest pain")
    ticket = db.query(SupportConversation).one()
    agent = make_user(db, "patient")
    SupportService(db).resolve(ticket.id, agent.id)
    db.commit()
    db.expire_all()
    assert db.get(SupportConversation, ticket.id).status == "resolved"
    assert chat.conversation.automation_paused is False and chat.state == "idle"
    assert chat.say("menu")[0]["interactive"]["type"] == "list"


def test_resolving_a_data_withdrawal_ticket_closes_it_and_leaves_the_contact_without_consent(chat, db):
    onboarded(chat)
    chat.say("STOP DATA")
    ticket = db.query(SupportConversation).one()
    SupportService(db).resolve(ticket.id, make_user(db, "patient").id)
    db.commit()
    db.expire_all()
    assert db.get(SupportConversation, ticket.id).status == "resolved"
    assert chat.state == "declined"
    assert not ConsentService(db).has_required_consent(contact_id=chat.contact.id)


# --- privacy rules ---------------------------------------------------------------------------
SENSITIVE = ["chest pain", "my mother Folake", "metformin", "blood pressure 180/120", "diabetes"]


def test_no_message_body_is_ever_persisted(chat, db):
    onboarded(chat)
    chat.say("my mother Folake has chest pain")
    chat.say("I take metformin for diabetes, blood pressure 180/120")
    chat.say("qwerty zzz secret-phrase-123")
    dump = []
    for table in ("whatsapp_contacts", "whatsapp_conversations", "whatsapp_messages", "support_conversations",
                  "consents", "care_recipients", "care_relationships"):
        for row in db.execute(text(f"SELECT t::text FROM {table} t")).all():
            dump.append(row[0])
    blob = " ".join(dump).lower()
    for needle in SENSITIVE + ["secret-phrase-123", "qwerty"]:
        assert needle.lower() not in blob, needle


def test_whatsapp_messages_has_no_body_column(db):
    cols = {r[0] for r in db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'whatsapp_messages'")).all()}
    assert not cols & {"body", "text", "content", "message", "payload"}


def test_outbound_messages_never_echo_the_users_words_or_contain_health_data(chat, db, fake_meta):
    onboarded(chat)
    chat.say("my mother Folake has chest pain and takes metformin for diabetes")
    chat.say("what should I take for headache")
    chat.say("asdf zzz-unique-token")
    sent = join(fake_meta.bodies).lower()
    for needle in ["folake", "metformin", "diabetes", "zzz-unique-token", "headache", "mother"]:
        assert needle not in sent, needle


def test_every_outbound_message_is_recorded_as_metadata(chat, db):
    onboarded(chat)
    rows = db.query(WhatsAppMessage).order_by(WhatsAppMessage.created_at).all()
    assert [r.direction for r in rows].count("outbound") == 2
    assert all(r.meta_message_id for r in rows if r.direction == "outbound")
    assert all(r.status == "sent" for r in rows if r.direction == "outbound")


def test_logs_do_not_contain_the_phone_number_or_message_text(chat, db, caplog):
    with caplog.at_level("DEBUG"):
        chat.say("Hi")
        chat.say("my mother has chest pain")
    assert chat.wa_id not in caplog.text and "chest pain" not in caplog.text


# --- idempotency and failure handling --------------------------------------------------------
def test_processing_the_same_job_twice_changes_nothing_the_second_time(chat, db, fake_meta):
    payload = text_payload(chat.wa_id, "Hi", message_id="wamid.ONCE")
    jobs = ingest(db, parse_webhook(payload)).jobs
    process_inbound(db, jobs[0])
    count = len(fake_meta.bodies)
    process_inbound(db, jobs[0])
    assert len(fake_meta.bodies) == count
    assert db.query(WhatsAppMessage).filter_by(meta_message_id="wamid.ONCE").one().processed_at is not None


def test_a_failed_send_is_recorded_but_the_state_still_advances(chat, db, fake_meta):
    from app.tests.fake_meta import error_response

    fake_meta.script(error_response(400, 131047))
    chat.say("Hi")
    assert chat.state == "consent_pending"
    row = db.query(WhatsAppMessage).filter_by(direction="outbound").one()
    assert (row.status, row.error_code) == ("failed", "131047")


def test_an_engine_crash_rolls_everything_back_so_the_job_can_retry(chat, db, fake_meta, monkeypatch):
    from app.channels.whatsapp import processor

    monkeypatch.setattr(processor, "step", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    jobs = ingest(db, parse_webhook(text_payload(chat.wa_id, "Hi", message_id="wamid.CRASH"))).jobs
    with pytest.raises(RuntimeError):
        process_inbound(db, jobs[0])
    db.rollback()
    row = db.query(WhatsAppMessage).filter_by(meta_message_id="wamid.CRASH").one()
    assert row.processed_at is None and not fake_meta.bodies
    monkeypatch.undo()
    process_inbound(db, jobs[0])
    assert chat.state == "consent_pending"


def test_a_blocked_contact_gets_no_reply(chat, db, fake_meta):
    chat.say("Hi")
    contact = chat.contact
    contact.blocked = True
    db.commit()
    assert chat.say("hello") == []
