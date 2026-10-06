"""W1: the Meta Cloud API client against the fake Graph API (nothing leaves the machine)."""

import httpx
import pytest

from app.channels.whatsapp.messages import (
    ButtonsMessage, CtaUrlMessage, FlowMessage, ListMessage, ListRow, ListSection, LocationRequestMessage,
    TemplateMessage, TextMessage,
)
from app.channels.whatsapp.meta_client import MetaCloudClient
from app.channels.whatsapp.outbound import send_and_record
from app.db.models.whatsapp import WhatsAppMessage
from app.tests.factories_whatsapp import make_contact
from app.tests.fake_meta import TEST_ACCESS_TOKEN, error_response, fake_meta  # noqa: F401  (fixture)

TO = "2348012345678"


def client() -> MetaCloudClient:
    return MetaCloudClient.from_settings()


def test_send_text_posts_the_documented_payload_to_the_configured_base_url(fake_meta):
    result = client().send(TO, TextMessage("Hello"))
    assert result.ok and result.meta_message_id == fake_meta.ids[0] and result.attempts == 1
    request = fake_meta.requests[0]
    assert str(request.url) == "http://graph.test/v23.0/100000000000001/messages"
    assert request.headers["authorization"] == f"Bearer {TEST_ACCESS_TOKEN}"
    assert fake_meta.bodies[0] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": TO,
                                   "type": "text", "text": {"preview_url": False, "body": "Hello"}}


def test_interactive_buttons_payload(fake_meta):
    client().send(TO, ButtonsMessage("Pick", (("a:1", "One"), ("a:2", "Two"))))
    assert fake_meta.bodies[0]["interactive"] == {
        "type": "button", "body": {"text": "Pick"},
        "action": {"buttons": [{"type": "reply", "reply": {"id": "a:1", "title": "One"}},
                               {"type": "reply", "reply": {"id": "a:2", "title": "Two"}}]}}


def test_interactive_list_payload(fake_meta):
    msg = ListMessage("Menu", "Open", (ListSection("Main", (ListRow("m:1", "First"), ListRow("m:2", "Second",
                                                                                           "desc"))),))
    client().send(TO, msg)
    interactive = fake_meta.bodies[0]["interactive"]
    assert interactive["type"] == "list" and interactive["action"]["button"] == "Open"
    assert interactive["action"]["sections"] == [{"title": "Main", "rows": [
        {"id": "m:1", "title": "First"}, {"id": "m:2", "title": "Second", "description": "desc"}]}]


def test_cta_url_location_request_flow_and_template_payloads(fake_meta):
    c = client()
    c.send(TO, CtaUrlMessage("Pay", "Pay now", "https://example.test/pay"))
    c.send(TO, LocationRequestMessage("Share your location"))
    c.send(TO, FlowMessage("Open the form", flow_id="FLOW1", flow_token="tok", cta="Start"))
    c.send(TO, TemplateMessage("booking_paid", "en", components=({"type": "body", "parameters": [
        {"type": "text", "text": "OK-1"}]},), category="utility"))
    cta, loc, flow, template = fake_meta.bodies
    assert cta["interactive"]["type"] == "cta_url"
    assert cta["interactive"]["action"] == {"name": "cta_url", "parameters": {
        "display_text": "Pay now", "url": "https://example.test/pay"}}
    assert loc["interactive"]["action"] == {"name": "send_location"}
    assert flow["interactive"]["type"] == "flow"
    assert flow["interactive"]["action"]["parameters"]["flow_id"] == "FLOW1"
    assert template["type"] == "template" and template["template"]["name"] == "booking_paid"
    assert template["template"]["language"] == {"code": "en"}


@pytest.mark.parametrize("bad", [
    lambda: ButtonsMessage("x", ()),
    lambda: ButtonsMessage("x", tuple((f"i:{n}", "t") for n in range(4))),
    lambda: ButtonsMessage("x", (("i:1", "a title that is far too long"),)),
    lambda: TextMessage(""),
    lambda: TextMessage("x" * 4097),
    lambda: ListMessage("m", "a button label too long", (ListSection("s", (ListRow("a:1", "t"),)),)),
])
def test_invalid_messages_are_refused_before_anything_is_sent(bad):
    with pytest.raises(ValueError):
        bad()


def test_429_then_5xx_then_success_retries_with_backoff(fake_meta, monkeypatch):
    from app.channels.whatsapp import meta_client

    waits = []
    monkeypatch.setattr(meta_client, "_sleep", waits.append)
    fake_meta.script(error_response(429, 130429, headers={"Retry-After": "2"}), error_response(503, 1))
    result = client().send(TO, TextMessage("Hello"))
    assert result.ok and result.attempts == 3 and len(fake_meta.requests) == 3
    assert len(waits) == 2 and waits[0] == 2.0   # Retry-After is honoured, then exponential backoff
    assert waits[1] > 0


def test_gives_up_after_the_configured_attempts_and_reports_the_error_code(fake_meta):
    fake_meta.script(*[error_response(500, 2) for _ in range(5)])
    result = client().send(TO, TextMessage("Hello"))
    assert not result.ok and result.attempts == 3 and result.error_code == "2"
    assert len(fake_meta.requests) == 3


@pytest.mark.parametrize("status,code", [(400, 131047), (400, 131026), (401, 190), (403, 10)])
def test_terminal_4xx_is_not_retried(fake_meta, status, code):
    fake_meta.script(error_response(status, code))
    result = client().send(TO, TextMessage("Hello"))
    assert not result.ok and result.attempts == 1 and result.error_code == str(code)
    assert len(fake_meta.requests) == 1


def test_connection_failures_are_retried_but_a_read_timeout_is_not(fake_meta):
    """A read timeout is ambiguous: Meta may have accepted the message, and a retry would send it twice."""
    fake_meta.script(httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"))
    result = client().send(TO, TextMessage("Hello"))
    assert result.ok and result.attempts == 3

    fake_meta.script(httpx.ReadTimeout("no answer"))
    result = client().send(TO, TextMessage("Hello"))
    assert not result.ok and result.attempts == 1 and result.error_code == "timeout"


def test_the_token_never_appears_in_a_failure_result_or_log(fake_meta, caplog):
    fake_meta.script(*[error_response(500, 2) for _ in range(3)])
    with caplog.at_level("DEBUG"):
        result = client().send(TO, TextMessage("Hello"))
    assert TEST_ACCESS_TOKEN not in repr(result)
    assert TEST_ACCESS_TOKEN not in caplog.text
    assert TO not in caplog.text  # recipient numbers are masked in logs


def test_send_and_record_stores_metadata_only(db, fake_meta):
    contact = make_contact(db)
    result = send_and_record(db, client(), contact, TextMessage("Some words that must not be stored"))
    db.expire_all()
    row = db.query(WhatsAppMessage).one()
    assert row.meta_message_id == result.meta_message_id
    assert (row.direction, row.type, row.status, row.error_code) == ("outbound", "text", "sent", None)
    assert "Some words" not in repr(vars(row))


def test_send_and_record_stores_the_template_and_category(db, fake_meta):
    contact = make_contact(db)
    send_and_record(db, client(), contact, TemplateMessage("booking_paid", "en", category="utility"))
    row = db.query(WhatsAppMessage).one()
    assert (row.type, row.template_name, row.category) == ("template", "booking_paid", "utility")


def test_a_terminal_failure_is_recorded_on_the_message_row(db, fake_meta):
    contact = make_contact(db)
    fake_meta.script(error_response(400, 131047))
    result = send_and_record(db, client(), contact, TextMessage("Hello"))
    assert not result.ok
    row = db.query(WhatsAppMessage).one()
    assert (row.direction, row.status, row.error_code, row.meta_message_id) == ("outbound", "failed", "131047", None)


def test_a_status_callback_that_beat_the_send_record_is_not_lost(db, fake_meta):
    """Meta can deliver the 'delivered' callback before our send call has been written down."""
    from app.channels.whatsapp.ingest import ingest, parse_webhook
    from app.tests.fake_meta import status_payload

    contact = make_contact(db)
    ingest(db, parse_webhook(status_payload(contact.wa_id, "wamid.EARLY", "delivered")))
    fake_meta.ids.clear()
    fake_meta.script(httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.EARLY"}]}))
    send_and_record(db, client(), contact, TextMessage("Hello"))
    db.expire_all()
    row = db.query(WhatsAppMessage).one()
    assert row.meta_message_id == "wamid.EARLY" and row.status == "delivered" and row.type == "text"
