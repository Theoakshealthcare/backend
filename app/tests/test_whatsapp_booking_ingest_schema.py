"""W2: how the webhook reads Flow completions, template button taps and location pins; the flow token; the new tables."""

import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.channels.whatsapp.flows import token as flow_token
from app.channels.whatsapp.ingest import ingest, parse_webhook
from app.channels.whatsapp.queue import BODY_FIELDS
from app.core.config import settings
from app.db.models.whatsapp import WhatsAppConversation
from app.db.models.whatsapp_booking import BookingEvent, Quote
from app.tests.factories import make_booking, make_patient
from app.tests.factories_whatsapp import make_contact, make_wa_id
from app.tests.fake_meta import (
    _envelope, _message_value, flow_reply_payload, location_payload, template_button_payload,
)


def job_for(payload):
    parsed = parse_webhook(payload, expected_phone_number_id="")
    assert len(parsed.messages) == 1
    return parsed.messages[0]


# --- webhook parsing -------------------------------------------------------------------------------
def test_a_flow_completion_yields_only_the_flow_token():
    envelope = job_for(flow_reply_payload(make_wa_id(), "draft.123.sig"))
    assert (envelope.type, envelope.interactive_type, envelope.reply_id) == ("interactive", "nfm_reply", "draft.123.sig")
    assert envelope.text is None


def test_anything_else_in_the_completion_payload_is_ignored_and_never_queued(db):
    """Even if a Flow were changed to return answers, they must not travel through the webhook and the queue."""
    wa_id = make_wa_id()
    payload = _envelope(_message_value(wa_id, "X", {"type": "interactive", "interactive": {
        "type": "nfm_reply", "nfm_reply": {"name": "flow", "body": "Sent", "response_json": json.dumps({
            "flow_token": "tok.1.sig", "condition": "SENTINEL-LEAK", "address": "SENTINEL-ADDRESS"})}}}))
    result = ingest(db, parse_webhook(payload, expected_phone_number_id=""))
    assert "SENTINEL" not in json.dumps(result.jobs) and result.jobs[0]["reply_id"] == "tok.1.sig"


@pytest.mark.parametrize("response_json", [None, "", "not json", "[1,2]", json.dumps({"flow_token": 5}),
                                           json.dumps({"flow_token": ""}), json.dumps({"flow_token": "x" * 300}),
                                           "{" * 5000, json.dumps({})])
def test_a_malformed_completion_has_no_token_and_does_not_raise(response_json):
    payload = _envelope(_message_value(make_wa_id(), "X", {"type": "interactive", "interactive": {
        "type": "nfm_reply", "nfm_reply": {"name": "flow", "response_json": response_json}}}))
    assert job_for(payload).reply_id is None


def test_a_template_quick_reply_tap_is_read_as_a_button():
    envelope = job_for(template_button_payload(make_wa_id(), "menu:book", "Book again"))
    assert (envelope.type, envelope.interactive_type, envelope.reply_id) == ("button", "template_button", "menu:book")
    assert envelope.text is None  # the visible label is not kept


def test_a_location_pin_carries_coordinates_to_the_worker_only(db):
    result = ingest(db, parse_webhook(location_payload(make_wa_id(), 6.4474, 3.4723), expected_phone_number_id=""))
    assert (result.jobs[0]["latitude"], result.jobs[0]["longitude"]) == (6.4474, 3.4723)
    assert {"latitude", "longitude"} <= set(BODY_FIELDS)  # they ride the short-lived body key, not the ARQ job
    assert "6.4474" not in str(db.execute(text("SELECT m::text FROM whatsapp_messages m")).fetchall())


@pytest.mark.parametrize("location", [{}, {"latitude": "6.4", "longitude": "x"}, {"latitude": True, "longitude": None}, "str"])
def test_hostile_location_values_become_no_coordinates(location):
    payload = _envelope(_message_value(make_wa_id(), "X", {"type": "location", "location": location}))
    envelope = job_for(payload)
    assert envelope.latitude is None and envelope.longitude is None


# --- the flow token -------------------------------------------------------------------------------------
def test_a_token_round_trips_and_names_its_draft():
    draft = uuid.uuid4()
    assert flow_token.verify(flow_token.issue(draft)) == draft


def test_a_token_expires():
    now = datetime(2026, 10, 6, 12, 0)
    token = flow_token.issue(uuid.uuid4(), now)
    assert flow_token.verify(token, now + timedelta(minutes=59)) is not None
    assert flow_token.verify(token, now + timedelta(minutes=61)) is None


def test_a_token_cannot_be_altered_or_moved_to_another_draft():
    draft, other = uuid.uuid4(), uuid.uuid4()
    token = flow_token.issue(draft)
    _, expires, signature = token.split(".")
    assert flow_token.verify(f"{other}.{expires}.{signature}") is None
    assert flow_token.verify(f"{draft}.{int(expires) + 99999}.{signature}") is None
    assert flow_token.verify(token[:-1] + ("A" if token[-1] != "A" else "B")) is None


@pytest.mark.parametrize("junk", [None, "", 5, "a.b", "a.b.c.d", "x" * 500, "not-a-uuid.123.sig", {}, []])
def test_junk_is_not_a_token(junk):
    assert flow_token.verify(junk) is None


def test_a_token_made_under_another_secret_key_is_rejected(monkeypatch):
    token = flow_token.issue(uuid.uuid4())
    monkeypatch.setattr(settings, "SECRET_KEY", "a-different-secret-key")
    assert flow_token.verify(token) is None


# --- the new tables and columns ---------------------------------------------------------------------------
def test_a_quote_becomes_at_most_one_booking(db):
    contact = make_contact(db)
    booking = make_booking(db, make_patient(db))
    db.add(Quote(contact_id=contact.id, status="confirmed", booking_id=booking.id, total_kobo=1))
    db.commit()
    db.add(Quote(contact_id=contact.id, status="confirmed", booking_id=booking.id, total_kobo=1))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("fields", [{"status": "bogus"}, {"total_kobo": 0}, {"total_kobo": -5}])
def test_quote_constraints(db, fields):
    contact = make_contact(db)
    db.add(Quote(contact_id=contact.id, **{"status": "quoted", **fields}))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_quote_needs_a_real_contact(db):
    db.add(Quote(contact_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_an_app_booking_defaults_to_the_app_channel_and_nothing_is_flagged(db):
    booking = make_booking(db, make_patient(db))
    assert (booking.channel, booking.booked_by_user_id, booking.care_recipient_id) == ("app", None, None)
    assert booking.needs_address_check is False and booking.condition_encrypted is None


def test_the_channel_is_one_of_app_whatsapp_admin(db):
    make_booking(db, make_patient(db), channel="whatsapp")
    make_booking(db, make_patient(db), channel="admin")
    with pytest.raises(IntegrityError):
        make_booking(db, make_patient(db), channel="carrier-pigeon")
    db.rollback()


def test_one_event_per_booking_and_name(db):
    booking = make_booking(db, make_patient(db), channel="whatsapp")
    db.add(BookingEvent(booking_id=booking.id, event="payment_received"))
    db.commit()
    db.add(BookingEvent(booking_id=booking.id, event="payment_received"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(BookingEvent(booking_id=booking.id, event="not_an_event"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("state", ["booking_flow", "awaiting_location", "quoted", "awaiting_payment"])
def test_the_conversation_accepts_the_booking_states(db, state):
    contact = make_contact(db)
    conversation = db.query(WhatsAppConversation).filter_by(contact_id=contact.id).one()
    conversation.state = state
    db.commit()
    conversation.state = "nonsense"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
