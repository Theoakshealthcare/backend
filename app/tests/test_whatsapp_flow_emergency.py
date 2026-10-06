"""W2 / WA-FR-40: words typed inside the booking Flow are screened for emergencies like any inbound message."""

import json
from datetime import timedelta

import pytest

from app.channels.whatsapp import booking_rules as rules
from app.db.models.support import SupportConversation
from app.db.models.whatsapp_booking import Quote
from app.tests.whatsapp_booking_helpers import (  # noqa: F401  (fixtures)
    Chat, FlowDriver, flow_reply_payload, flow_token_from, onboarded, rules_now,
)

EMERGENCY = {"duration": "2", "condition": "my father is unconscious and not breathing", "instructions": ""}


@pytest.fixture
def chat(db, fake_meta):
    return Chat(db, fake_meta)


@pytest.fixture
def flow(db, client, chat, booking_env):
    onboarded(chat)
    chat.pick("menu:book")
    return FlowDriver(client, booking_env.public, flow_token_from(chat.fake.bodies))


def to_details(flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "nurse"})
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    flow.exchange("WHEN", {"trigger": "submit", "date": day, "time": "10:00"})


def test_an_emergency_in_the_condition_box_gets_the_emergency_notice_first_without_echoing_it(db, flow):
    to_details(flow)
    _, body = flow.exchange("DETAILS", EMERGENCY)
    assert body["screen"] == "DETAILS" and "112" in body["data"]["notice"]
    assert "father" not in json.dumps(body) and "breathing" not in json.dumps(body)
    db.expire_all()
    assert db.query(Quote).one().sensitive_encrypted is None  # nothing was kept on that first pass


def test_a_second_tap_goes_through_and_a_person_is_alerted_in_chat(db, flow, chat):
    to_details(flow)
    flow.exchange("DETAILS", EMERGENCY)
    _, body = flow.exchange("DETAILS", EMERGENCY)
    assert body["screen"] == "ADDRESS"
    flow.exchange("ADDRESS", {"street": "1 Rd", "area": "Yaba", "city": "Lagos"})
    sent = chat.send(flow_reply_payload(chat.wa_id, flow.token))
    assert sent and chat.state == "awaiting_location"  # the booking carries on
    ticket = db.query(SupportConversation).one()
    assert (ticket.reason, ticket.priority) == ("emergency", "urgent")


def test_emergency_words_in_the_extra_notes_count_too(db, flow):
    to_details(flow)
    _, body = flow.exchange("DETAILS", {"duration": "2", "condition": "wound dressing",
                                        "instructions": "she is having a seizure right now"})
    assert body["screen"] == "DETAILS" and "112" in body["data"]["notice"]


def test_an_ordinary_condition_is_not_stopped_and_opens_no_ticket(db, flow, chat):
    to_details(flow)
    _, body = flow.exchange("DETAILS", {"duration": "2", "condition": "post-surgery wound dressing, mobility help",
                                        "instructions": "second floor, ring the bell"})
    assert body["screen"] == "ADDRESS"
    flow.exchange("ADDRESS", {"street": "1 Rd", "area": "Yaba", "city": "Lagos"})
    chat.send(flow_reply_payload(chat.wa_id, flow.token))
    assert db.query(SupportConversation).count() == 0
