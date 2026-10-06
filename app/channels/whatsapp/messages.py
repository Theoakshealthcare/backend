"""Outbound message types and their Cloud API payloads.

Each type validates WhatsApp's documented limits when it is built, so an over-long button title fails
in a unit test instead of as a 400 from Meta in production.
Reference: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
and https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-reply-buttons-messages
"""

from dataclasses import dataclass, field
from typing import Any, NamedTuple, Optional, Tuple, Union

MAX_TEXT = 4096
MAX_INTERACTIVE_BODY = 1024
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_LIST_ROWS = 10
MAX_ROW_TITLE = 24
MAX_LIST_BUTTON = 20


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class TextMessage:
    body: str
    preview_url: bool = False
    kind = "text"

    def __post_init__(self):
        _require(1 <= len(self.body) <= MAX_TEXT, "text body must be 1-4096 characters")


@dataclass(frozen=True)
class ButtonsMessage:
    body: str
    buttons: Tuple[Tuple[str, str], ...]  # (id, title)
    kind = "interactive"

    def __post_init__(self):
        _require(1 <= len(self.body) <= MAX_INTERACTIVE_BODY, "interactive body must be 1-1024 characters")
        _require(1 <= len(self.buttons) <= MAX_BUTTONS, "1-3 reply buttons")
        for _, title in self.buttons:
            _require(1 <= len(title) <= MAX_BUTTON_TITLE, "button title must be 1-20 characters")


class ListRow(NamedTuple):
    id: str
    title: str
    description: Optional[str] = None


class ListSection(NamedTuple):
    title: str
    rows: Tuple[ListRow, ...]


@dataclass(frozen=True)
class ListMessage:
    body: str
    button_label: str
    sections: Tuple[ListSection, ...]
    kind = "interactive"

    def __post_init__(self):
        _require(1 <= len(self.body) <= MAX_INTERACTIVE_BODY, "interactive body must be 1-1024 characters")
        _require(1 <= len(self.button_label) <= MAX_LIST_BUTTON, "list button label must be 1-20 characters")
        rows = [r for s in self.sections for r in s.rows]
        _require(1 <= len(rows) <= MAX_LIST_ROWS, "1-10 list rows")
        for row in rows:
            _require(1 <= len(row.title) <= MAX_ROW_TITLE, "row title must be 1-24 characters")


@dataclass(frozen=True)
class CtaUrlMessage:
    body: str
    display_text: str
    url: str
    kind = "interactive"

    def __post_init__(self):
        _require(1 <= len(self.body) <= MAX_INTERACTIVE_BODY, "interactive body must be 1-1024 characters")
        _require(1 <= len(self.display_text) <= MAX_BUTTON_TITLE, "cta text must be 1-20 characters")
        _require(self.url.startswith("https://"), "cta url must be https")


@dataclass(frozen=True)
class LocationRequestMessage:
    body: str
    kind = "interactive"

    def __post_init__(self):
        _require(1 <= len(self.body) <= MAX_INTERACTIVE_BODY, "interactive body must be 1-1024 characters")


@dataclass(frozen=True)
class FlowMessage:
    body: str
    flow_id: str
    flow_token: str
    cta: str
    screen: Optional[str] = None
    data: dict = field(default_factory=dict)
    kind = "interactive"

    def __post_init__(self):
        _require(1 <= len(self.body) <= MAX_INTERACTIVE_BODY, "interactive body must be 1-1024 characters")
        _require(1 <= len(self.cta) <= MAX_BUTTON_TITLE, "flow cta must be 1-20 characters")


@dataclass(frozen=True)
class TemplateMessage:
    name: str
    language: str
    components: Tuple[dict, ...] = ()
    category: Optional[str] = None  # utility | authentication | marketing; recorded, not sent
    kind = "template"


OutboundMessage = Union[TextMessage, ButtonsMessage, ListMessage, CtaUrlMessage, LocationRequestMessage,
                        FlowMessage, TemplateMessage]


def to_payload(to: str, msg: OutboundMessage) -> dict[str, Any]:
    base: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
    if isinstance(msg, TextMessage):
        return {**base, "type": "text", "text": {"preview_url": msg.preview_url, "body": msg.body}}
    if isinstance(msg, TemplateMessage):
        template: dict[str, Any] = {"name": msg.name, "language": {"code": msg.language}}
        if msg.components:
            template["components"] = list(msg.components)
        return {**base, "type": "template", "template": template}

    interactive: dict[str, Any]
    if isinstance(msg, ButtonsMessage):
        interactive = {
            "type": "button", "body": {"text": msg.body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in msg.buttons]},
        }
    elif isinstance(msg, ListMessage):
        interactive = {
            "type": "list", "body": {"text": msg.body},
            "action": {"button": msg.button_label, "sections": [
                {"title": s.title, "rows": [
                    {"id": r.id, "title": r.title, **({"description": r.description} if r.description else {})}
                    for r in s.rows]}
                for s in msg.sections]},
        }
    elif isinstance(msg, CtaUrlMessage):
        interactive = {
            "type": "cta_url", "body": {"text": msg.body},
            "action": {"name": "cta_url", "parameters": {"display_text": msg.display_text, "url": msg.url}},
        }
    elif isinstance(msg, LocationRequestMessage):
        interactive = {"type": "location_request_message", "body": {"text": msg.body},
                       "action": {"name": "send_location"}}
    elif isinstance(msg, FlowMessage):
        parameters: dict[str, Any] = {"flow_message_version": "3", "flow_token": msg.flow_token,
                                      "flow_id": msg.flow_id, "flow_cta": msg.cta, "flow_action": "navigate"}
        if msg.screen:
            parameters["flow_action_payload"] = {"screen": msg.screen, "data": msg.data}
        interactive = {"type": "flow", "body": {"text": msg.body},
                       "action": {"name": "flow", "parameters": parameters}}
    else:  # pragma: no cover - the union is closed
        raise TypeError(f"unsupported message type {type(msg).__name__}")
    return {**base, "type": "interactive", "interactive": interactive}
