"""The message-template registry (PRD section 14) and the 24-hour window rule.

WhatsApp lets us send free text only while the person has written to us in the last 24 hours (the customer
service window, ``contact.last_inbound_at``). Outside it only an approved template goes through. Every
business-initiated message in this module is therefore defined once here, as a template, with a plain-text
rendering of the same wording for use inside the window:

    ``choose(name, params, in_window=...)``  ->  text/CTA/buttons inside the window, a TemplateMessage outside it

so the two never drift apart and the rule is one function (``within_window``) rather than a habit.

Rules for the copy (PRD 9.3, WA-NFR-6): utility wording only, no promotion, and no health data: the only
variables are money, a booking code, a date and a first-party URL token. The condition typed in the booking
Flow can never reach a template because no template has a slot for it.

Bodies are the draft copy from PRD section 14; submit them to Meta for approval as written, in category
UTILITY. Meta mirrors the status back to us in W3/Admin (``message_templates`` is not a table yet: this module is
the source of truth and the Meta approval status is tracked by hand until the Admin console has a page for it).
Meta's rules for templates: https://developers.facebook.com/docs/whatsapp/message-templates/guidelines
URL buttons take one variable that is appended to the template's base URL:
https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates#button-components
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from app.channels.whatsapp.messages import (
    ButtonsMessage, CtaUrlMessage, OutboundMessage, TemplateMessage, TextMessage,
)

WINDOW = timedelta(hours=24)
LANGUAGE = "en"
CHECKOUT_BASE = "https://checkout.paystack.com/"  # Paystack's hosted checkout; the access code is the suffix

UTILITY = "utility"


def within_window(last_inbound_at: Optional[datetime], now: datetime) -> bool:
    """True while free-form messages are allowed. Never inside the window for someone who has not written to us."""
    return last_inbound_at is not None and (now - last_inbound_at) < WINDOW


@dataclass(frozen=True)
class Template:
    name: str
    body: str                         # Meta's body with {{1}}.. placeholders (what is submitted for approval)
    params: Tuple[str, ...]           # what {{1}}, {{2}}.. mean, in order
    category: str = UTILITY
    url_button: Optional[Tuple[str, str]] = None      # (label, base url): one variable appended to the base
    quick_reply: Optional[Tuple[str, str]] = None     # (label, payload sent back when tapped)
    essential: bool = False           # may still be sent to someone who sent STOP (a paid booking, WA-FR-8)


TEMPLATES: Dict[str, Template] = {t.name: t for t in (
    Template(
        "booking_payment_link",
        "Your Oakly booking {{1}} for {{2}} on {{3}} is ready. Total: {{4}}. Complete payment within 30 minutes "
        "to confirm.",
        ("booking_code", "recipient_first_name", "when", "total"),
        url_button=("Pay now", CHECKOUT_BASE),
    ),
    Template(
        "payment_link_expiring",
        "Your payment link for booking {{1}} expires in 10 minutes.",
        ("booking_code",),
        url_button=("Pay now", CHECKOUT_BASE),
    ),
    Template(
        "payment_received",
        "Payment of {{1}} received for booking {{2}}. We're assigning a professional now.",
        ("total", "booking_code"),
        essential=True,
    ),
    Template(
        "booking_cancelled_unpaid",
        "Booking {{1}} was cancelled because payment wasn't completed.",
        ("booking_code",),
        quick_reply=("Book again", "menu:book"),
    ),
)}


def _fill(body: str, template: Template, params: Dict[str, str]) -> str:
    missing = [p for p in template.params if p not in params]
    if missing:
        raise ValueError(f"template {template.name} needs {missing}")
    text = body
    for index, key in enumerate(template.params, start=1):
        text = text.replace("{{%d}}" % index, str(params[key]))
    return text


def render_text(name: str, params: Dict[str, str]) -> str:
    """The template's wording with the variables filled in, for free-text use inside the window."""
    template = TEMPLATES[name]
    return _fill(template.body, template, params)


def as_template(name: str, params: Dict[str, str], *, url_suffix: Optional[str] = None) -> TemplateMessage:
    """The Cloud API template message: body variables in ``params`` order, plus the button component."""
    template = TEMPLATES[name]
    _fill(template.body, template, params)  # validates the variables
    components: list[dict[str, Any]] = []
    if template.params:
        components.append({"type": "body", "parameters": [
            {"type": "text", "text": str(params[key])} for key in template.params]})
    if template.url_button:
        if not url_suffix:
            raise ValueError(f"template {name} needs the payment link suffix")
        components.append({"type": "button", "sub_type": "url", "index": "0",
                           "parameters": [{"type": "text", "text": url_suffix}]})
    if template.quick_reply:
        components.append({"type": "button", "sub_type": "quick_reply", "index": "0",
                           "parameters": [{"type": "payload", "payload": template.quick_reply[1]}]})
    return TemplateMessage(name=name, language=LANGUAGE, components=tuple(components), category=template.category)


def choose(name: str, params: Dict[str, str], *, in_window: bool, url: Optional[str] = None) -> OutboundMessage:
    """What to send for ``name``: the interactive/free-text form inside the window, the template outside it.
    ``url`` is the full checkout link, for templates with a Pay now button."""
    template = TEMPLATES[name]
    if not in_window:
        suffix = None
        if template.url_button:
            if not url or not url.startswith(template.url_button[1]):
                raise ValueError(f"template {name} cannot carry this link")
            suffix = url[len(template.url_button[1]):]
        return as_template(name, params, url_suffix=suffix)
    text = render_text(name, params)
    if template.url_button:
        if not url:
            return TextMessage(text)
        return CtaUrlMessage(text, template.url_button[0], url)
    if template.quick_reply:
        return ButtonsMessage(text, ((template.quick_reply[1], template.quick_reply[0]),))
    return TextMessage(text)


def checkout_suffix(url: str) -> Optional[str]:
    return url[len(CHECKOUT_BASE):] if url.startswith(CHECKOUT_BASE) else None
