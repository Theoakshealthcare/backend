"""A fake Meta WhatsApp Cloud API: the Graph send endpoint, plus builders for inbound webhooks.

Nothing here touches the network. ``FakeMetaGraph.handle`` is plain request-in, response-out logic;
tests reach it through an ``httpx.MockTransport`` (the ``fake_meta`` fixture), and
``scripts/whatsapp_local_e2e.py`` serves the same logic over a local HTTP port for the Docker stack.

Webhook payload shapes follow Meta's documented format:
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
Send API (request and response):
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
Signature (``X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body, keyed by the app secret>``):
  https://developers.facebook.com/docs/graph-api/webhooks/getting-started#validate-payloads

All ids, numbers and names below are obviously fake placeholders.
"""

import hashlib
import hmac
import json
import threading
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import httpx
import pytest

from app.core.config import settings

TEST_APP_SECRET = "test-app-secret-placeholder"
TEST_VERIFY_TOKEN = "test-verify-token-placeholder"
TEST_ACCESS_TOKEN = "test-access-token-placeholder"
TEST_PHONE_NUMBER_ID = "100000000000001"
TEST_GRAPH_URL = "http://graph.test"
DISPLAY_PHONE = "15550001111"


# --- inbound webhook builders ----------------------------------------------------------------
def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def wamid() -> str:
    return "wamid." + uuid.uuid4().hex


def _envelope(value: dict, phone_number_id: str = TEST_PHONE_NUMBER_ID) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "200000000000002", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": DISPLAY_PHONE, "phone_number_id": phone_number_id},
            **value,
        }}]}],
    }


def _message_value(wa_id: str, name: str, message: dict) -> dict:
    return {
        "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
        "messages": [{"from": wa_id, "id": message.pop("id", None) or wamid(),
                      "timestamp": "1760000000", **message}],
    }


def text_payload(wa_id: str, text: str, *, name: str = "Test Person", message_id: Optional[str] = None,
                 referral: Optional[dict] = None, phone_number_id: str = TEST_PHONE_NUMBER_ID) -> dict:
    message: dict[str, Any] = {"id": message_id, "type": "text", "text": {"body": text}}
    if referral:
        message["referral"] = referral
    return _envelope(_message_value(wa_id, name, message), phone_number_id)


def button_payload(wa_id: str, reply_id: str, title: str = "x", *, name: str = "Test Person",
                   message_id: Optional[str] = None) -> dict:
    return _envelope(_message_value(wa_id, name, {
        "id": message_id, "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": reply_id, "title": title}},
    }))


def list_payload(wa_id: str, reply_id: str, title: str = "x", *, name: str = "Test Person",
                 message_id: Optional[str] = None) -> dict:
    return _envelope(_message_value(wa_id, name, {
        "id": message_id, "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": reply_id, "title": title}},
    }))


def location_payload(wa_id: str, lat: float = 6.45, lng: float = 3.47, *, name: str = "Test Person",
                     message_id: Optional[str] = None) -> dict:
    return _envelope(_message_value(wa_id, name, {
        "id": message_id, "type": "location", "location": {"latitude": lat, "longitude": lng},
    }))


def media_payload(wa_id: str, kind: str = "image", *, name: str = "Test Person",
                  message_id: Optional[str] = None) -> dict:
    return _envelope(_message_value(wa_id, name, {
        "id": message_id, "type": kind, kind: {"id": "media-placeholder", "mime_type": "image/jpeg"},
    }))


def status_payload(wa_id: str, message_id: str, status: str, *, errors: Optional[list] = None,
                   category: Optional[str] = None) -> dict:
    entry: dict[str, Any] = {"id": message_id, "status": status, "timestamp": "1760000001",
                             "recipient_id": wa_id}
    if category:
        entry["pricing"] = {"billable": True, "pricing_model": "CBP", "category": category}
    if errors:
        entry["errors"] = errors
    return _envelope({"statuses": [entry]})


def body_bytes(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def signed_headers(body: bytes, secret: str = TEST_APP_SECRET) -> dict:
    return {"X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"}


# --- the fake Graph API ----------------------------------------------------------------------
class FakeMetaGraph:
    """Records every send request. ``script(...)`` queues responses (an ``httpx.Response`` or an
    exception to raise) that are consumed before the default success reply."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.ids: list[str] = []  # the wamid handed back for each successful send, in order
        self._scripted: deque = deque()

    def script(self, *items) -> None:
        self._scripted.extend(items)

    def handle(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
        """Pure core, shared with the local HTTP server. Returns (status, json body)."""
        payload = json.loads(body or b"{}")
        self.bodies.append(payload)
        if not headers.get("authorization", "").startswith("Bearer ") or not path.endswith("/messages"):
            return 400, {"error": {"message": "bad request", "code": 100}}
        to = payload.get("to", "")
        message_id = wamid()
        self.ids.append(message_id)
        return 200, {"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to}],
                     "messages": [{"id": message_id}]}

    def transport_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._scripted:
            item = self._scripted.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        status, payload = self.handle(request.method, request.url.path,
                                      {k.lower(): v for k, v in request.headers.items()}, request.content)
        return httpx.Response(status, json=payload)

    def texts(self) -> list[str]:
        """Every human-readable string in everything sent (body, button titles, row titles...)."""
        out: list[str] = []

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("body", "title", "text", "description", "display_text", "button") \
                            and isinstance(value, str):
                        out.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.bodies)
        return out

    def reply_ids(self) -> list[str]:
        ids: list[str] = []

        def walk(node):
            if isinstance(node, dict):
                value = node.get("id")
                if isinstance(value, str) and ":" in value:
                    ids.append(value)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.bodies)
        return ids


def error_response(status: int, code: int = 131026, message: str = "failed", headers: Optional[dict] = None):
    return httpx.Response(status, json={"error": {"message": message, "code": code}}, headers=headers or {})


@pytest.fixture
def fake_meta(monkeypatch):
    from app.channels.whatsapp import meta_client

    fake = FakeMetaGraph()
    monkeypatch.setattr(meta_client, "_transport", httpx.MockTransport(fake.transport_handler))
    monkeypatch.setattr(meta_client, "_sleep", lambda seconds: None)  # no real backoff waits in tests
    for name, value in {
        "WHATSAPP_APP_SECRET": TEST_APP_SECRET,
        "WHATSAPP_VERIFY_TOKEN": TEST_VERIFY_TOKEN,
        "WHATSAPP_ACCESS_TOKEN": TEST_ACCESS_TOKEN,
        "WHATSAPP_PHONE_NUMBER_ID": TEST_PHONE_NUMBER_ID,
        "WHATSAPP_GRAPH_URL": TEST_GRAPH_URL,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return fake


class FakeInboundQueue:
    def __init__(self):
        self.jobs: list[dict] = []
        self.fail: Optional[Exception] = None
        self.fail_after_write: Optional[Exception] = None  # the job is stored, then the call raises

    async def enqueue(self, job: dict) -> None:
        if self.fail is not None:
            raise self.fail
        if self.fail_after_write is not None:
            self.stored = getattr(self, "stored", []) + [job]  # what Redis would have kept; never delivered
            raise self.fail_after_write
        self.jobs.append(job)


@pytest.fixture
def inbound_queue():
    from app.channels.whatsapp.queue import get_inbound_queue
    from app.main import app

    queue = FakeInboundQueue()
    app.dependency_overrides[get_inbound_queue] = lambda: queue
    try:
        yield queue
    finally:
        app.dependency_overrides.pop(get_inbound_queue, None)


# --- the same fake as a local HTTP server (for the Docker end-to-end script) -----------------
def serve_fake_meta(graph: FakeMetaGraph, host: str = "0.0.0.0", port: int = 9911) -> ThreadingHTTPServer:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server API)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            with lock:
                status, payload = graph.handle("POST", self.path.split("?")[0],
                                               {k.lower(): v for k, v in self.headers.items()}, body)
            out = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):  # keep the script output readable
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
