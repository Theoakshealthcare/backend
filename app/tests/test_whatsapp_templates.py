"""W2: the template registry (PRD 14) and the 24-hour window rule."""

from datetime import datetime, timedelta

import pytest

from app.channels.whatsapp.messages import ButtonsMessage, CtaUrlMessage, TemplateMessage, TextMessage
from app.channels.whatsapp.meta_client import MetaCloudClient
from app.channels.whatsapp.outbound import WINDOW_CLOSED, send_and_record
from app.channels.whatsapp.templates import (
    CHECKOUT_BASE, TEMPLATES, WINDOW, as_template, checkout_suffix, choose, render_text, within_window,
)
from app.db.models.whatsapp import WhatsAppMessage
from app.tests.factories_whatsapp import make_contact

NOW = datetime(2026, 10, 6, 12, 0, 0)
URL = CHECKOUT_BASE + "ac_abc123"


# --- the registry ----------------------------------------------------------------------------------------
def test_the_w2_templates_exist_in_the_utility_category_with_the_prd_wording():
    assert set(TEMPLATES) == {"booking_payment_link", "payment_link_expiring", "payment_received",
                              "payment_refund_pending", "booking_cancelled_unpaid"}
    assert all(t.category == "utility" for t in TEMPLATES.values())
    assert TEMPLATES["payment_received"].body == (
        "Payment of {{1}} received for booking {{2}}. We're assigning a professional now.")
    assert TEMPLATES["payment_link_expiring"].body == (
        "Your payment link for booking {{1}} expires in {{2}} minutes.")
    assert TEMPLATES["payment_refund_pending"].body == (
        "We received your payment of {{1}} for booking {{2}}, but that booking had already been cancelled. "
        "We'll refund you; our team will be in touch.")
    assert TEMPLATES["booking_cancelled_unpaid"].body == "Booking {{1}} was cancelled because payment wasn't completed."
    assert TEMPLATES["booking_payment_link"].body.startswith("Your Oakly booking {{1}} for {{2}} on {{3}}")


def test_variable_placeholders_match_the_declared_parameters():
    import re
    for template in TEMPLATES.values():
        numbers = sorted(int(n) for n in re.findall(r"\{\{(\d+)\}\}", template.body))
        assert numbers == list(range(1, len(template.params) + 1)), template.name


def test_no_template_has_a_slot_for_health_information():
    banned = ("condition", "diagnosis", "medication", "symptom", "note", "instruction", "address", "surname")
    for template in TEMPLATES.values():
        assert not any(b in p for p in template.params for b in banned), template.name


def test_only_the_messages_about_money_taken_are_essential():
    assert [t.name for t in TEMPLATES.values() if t.essential] == ["payment_received", "payment_refund_pending"]


def test_render_fills_in_order_and_refuses_a_missing_variable():
    assert render_text("payment_received", {"total": "₦44,000", "booking_code": "AB12CD34"}) == \
        "Payment of ₦44,000 received for booking AB12CD34. We're assigning a professional now."
    with pytest.raises(ValueError):
        render_text("payment_received", {"total": "₦44,000"})


def test_every_free_text_rendering_fits_whatsapps_limits():
    params = {"booking_code": "AB12CD34", "minutes": "10", "total": "₦1,000,000.50", "recipient_first_name": "Oluwatobiloba",
              "when": "Sat 4 Oct, 10:00–14:00"}
    for name in TEMPLATES:
        message = choose(name, params, in_window=True, url=URL)  # building it validates the limits
        assert isinstance(message, (TextMessage, ButtonsMessage, CtaUrlMessage))


# --- inside the window: free text; outside: the template ------------------------------------------------
def test_inside_the_window_the_same_words_go_as_free_text_with_buttons():
    pay = choose("payment_link_expiring", {"booking_code": "AB12", "minutes": "10"}, in_window=True, url=URL)
    assert isinstance(pay, CtaUrlMessage) and pay.display_text == "Pay now" and pay.url == URL
    again = choose("booking_cancelled_unpaid", {"booking_code": "AB12"}, in_window=True)
    assert isinstance(again, ButtonsMessage) and again.buttons == (("menu:book", "Book again"),)
    receipt = choose("payment_received", {"total": "₦1", "booking_code": "AB12"}, in_window=True)
    assert isinstance(receipt, TextMessage) and receipt.body.startswith("Payment of ₦1 received")


def test_outside_the_window_it_is_a_template_message():
    message = choose("payment_received", {"total": "₦44,000", "booking_code": "AB12"}, in_window=False)
    assert isinstance(message, TemplateMessage) and message.name == "payment_received" and message.language == "en"
    assert message.components == ({"type": "body", "parameters": [
        {"type": "text", "text": "₦44,000"}, {"type": "text", "text": "AB12"}]},)


def test_a_url_button_template_carries_only_the_suffix_of_the_checkout_link():
    message = as_template("payment_link_expiring", {"booking_code": "AB12", "minutes": "10"}, url_suffix="ac_abc123")
    assert message.components[1] == {"type": "button", "sub_type": "url", "index": "0",
                                     "parameters": [{"type": "text", "text": "ac_abc123"}]}
    assert checkout_suffix(URL) == "ac_abc123" and checkout_suffix("https://evil.example/x") is None


def test_a_template_will_not_carry_a_link_that_is_not_ours():
    with pytest.raises(ValueError):
        choose("payment_link_expiring", {"booking_code": "AB12", "minutes": "10"}, in_window=False, url="https://evil.example/pay")
    with pytest.raises(ValueError):
        choose("payment_link_expiring", {"booking_code": "AB12", "minutes": "10"}, in_window=False, url=None)


def test_the_quick_reply_template_sends_its_payload_back():
    message = choose("booking_cancelled_unpaid", {"booking_code": "AB12"}, in_window=False)
    assert message.components[1] == {"type": "button", "sub_type": "quick_reply", "index": "0",
                                     "parameters": [{"type": "payload", "payload": "menu:book"}]}


def test_a_template_payload_is_valid_cloud_api_json():
    from app.channels.whatsapp.messages import to_payload
    payload = to_payload("2348000000000", choose("payment_received", {"total": "₦1", "booking_code": "A"}, in_window=False))
    assert payload["type"] == "template" and payload["template"]["name"] == "payment_received"
    assert payload["template"]["language"] == {"code": "en"}


# --- the rule itself --------------------------------------------------------------------------------------
@pytest.mark.parametrize("ago,expected", [
    (timedelta(0), True), (timedelta(hours=23, minutes=59), True), (WINDOW, False), (timedelta(hours=24, seconds=1), False),
    (timedelta(days=3), False)])
def test_within_window(ago, expected):
    assert within_window(NOW - ago, NOW) is expected


def test_someone_who_never_wrote_to_us_is_never_inside_the_window():
    assert within_window(None, NOW) is False


# --- enforcement in the one function every send goes through ----------------------------------------------
def test_a_free_form_message_outside_the_window_is_not_sent_and_is_recorded_as_failed(db, fake_meta):
    contact = make_contact(db, last_inbound_at=NOW - timedelta(hours=30))
    result = send_and_record(db, MetaCloudClient.from_settings(), contact, TextMessage("hello"), now=NOW)
    assert result.ok is False and result.error_code == WINDOW_CLOSED
    assert fake_meta.requests == []  # Meta was never asked
    row = db.query(WhatsAppMessage).filter_by(contact_id=contact.id).one()
    assert (row.status, row.error_code, row.type) == ("failed", WINDOW_CLOSED, "text")


@pytest.mark.parametrize("message", [
    ButtonsMessage("hi", (("a:b", "Ok"),)), CtaUrlMessage("hi", "Pay", "https://x.test/p"),
])
def test_every_free_form_kind_is_held_back_outside_the_window(db, fake_meta, message):
    contact = make_contact(db, last_inbound_at=NOW - timedelta(days=2))
    assert send_and_record(db, MetaCloudClient.from_settings(), contact, message, now=NOW).error_code == WINDOW_CLOSED
    assert fake_meta.requests == []


def test_a_template_outside_the_window_is_sent(db, fake_meta):
    contact = make_contact(db, last_inbound_at=NOW - timedelta(days=2))
    message = choose("payment_received", {"total": "₦1", "booking_code": "A"}, in_window=False)
    result = send_and_record(db, MetaCloudClient.from_settings(), contact, message, now=NOW)
    assert result.ok and len(fake_meta.requests) == 1
    row = db.query(WhatsAppMessage).filter_by(contact_id=contact.id).one()
    assert (row.type, row.template_name, row.category) == ("template", "payment_received", "utility")


def test_free_text_inside_the_window_is_sent(db, fake_meta):
    contact = make_contact(db, last_inbound_at=NOW - timedelta(hours=2))
    assert send_and_record(db, MetaCloudClient.from_settings(), contact, TextMessage("hello"), now=NOW).ok


def test_a_contact_who_never_wrote_cannot_be_sent_free_text(db, fake_meta):
    contact = make_contact(db)
    contact.last_inbound_at = None
    db.commit()
    assert send_and_record(db, MetaCloudClient.from_settings(), contact, TextMessage("hello"), now=NOW).error_code \
        == WINDOW_CLOSED
