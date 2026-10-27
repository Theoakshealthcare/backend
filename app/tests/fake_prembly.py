"""A fake Prembly widget-session API (caregiver KYC), plus helpers to sign webhooks the way Prembly documents.

Nothing here touches the network. ``FakePrembly.handle`` is plain request-in, response-out logic; tests reach it through an
``httpx.MockTransport`` (the ``fake_prembly`` fixture), like ``fake_meta``. It is built from the PUBLIC docs only
(docs/plans/2026-10-27-caregiver-kyc-design.md section 3), so every shape marked UNCONFIRMED there is a guess here until the K0
spike and K8 live pass; update this file from the real captures.

Documented and followed:
  session wrapper     {"status": true, "message", "data": {"sessions": [...], "pagination": {...}}, "errors": null, "meta": {...}}
  session fields      id, session_id, status, full_name, end_user_email, end_user_phone, widget_name, overall_result,
                      failure_reason, total_cost, currency, started_at, completed_at, duration, created_at, is_used
  auth headers        x-api-key (and x-organisation-id on the API & Webhooks page)
  errors              HTTP 200 + ``response_code`` 01/02/03/07, HTTP 402 (wallet), 429 (wait ~60 s), 5xx
  webhook signature   ``x-prembly-signature`` = base64 HMAC-SHA256 of the raw body; ``token`` = a unique id per webhook
Guessed (UNCONFIRMED): the by-id response shape (served as the same wrapper with one session), the contents of ``payload``, the
``overall_result`` values besides "passed", the webhook body.

All ids, names and numbers below are obviously fake placeholders. The only ID numbers are the ones Prembly publishes for its
sandbox.
"""
import base64
import hashlib
import hmac
import json
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Optional

import httpx
import pytest

from app.core.config import settings

TEST_BASE_URL = "http://prembly.test"
TEST_SECRET_KEY = "test-prembly-secret-key-placeholder"
TEST_ORG_ID = "test-prembly-org-placeholder"
TEST_WEBHOOK_KEY = "test-prembly-webhook-key-placeholder"
TEST_FINGERPRINT_KEY = "test-kyc-fingerprint-key-placeholder"
SANDBOX_NIN = "12345678909"  # Prembly's published test NIN
SANDBOX_BVN = "54651333604"  # Prembly's published test BVN
SESSIONS_PATH = "/api/v1/checker-widget/sdk/sessions/"


# --- webhook builders ------------------------------------------------------------------------
def sign(body: bytes, key: str) -> str:
    """``x-prembly-signature``: base64 of HMAC-SHA256 over the raw body (docs/authorization-and-security)."""
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def webhook_payload(session_id: str, *, event: str = "verification.completed", status: str = "VERIFIED",
                    oakly_ref: Optional[str] = None, face_confidence: Optional[float] = None) -> dict:
    """The SDK webhook sample from the docs (UNCONFIRMED: whether ``metadata`` is echoed, and where)."""
    data: dict[str, Any] = {"verification_response": {}, "biometric_results": {},
                            "widget_info": {"email": "placeholder@example.com", "first_name": "Test", "last_name": "Person",
                                            "user_ref": None}}
    if face_confidence is not None:
        data["face_data"] = {"status": "success", "detail": "placeholder", "response_code": "00", "confidence": face_confidence}
    if oakly_ref is not None:
        data["metadata"] = {"oakly_ref": oakly_ref}
    return {"event": event, "session_id": session_id, "data": data, "verification": {"status": status}}


def body_bytes(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def signed_webhook(payload: dict, key: str = TEST_WEBHOOK_KEY, *, token: Optional[str] = None) -> tuple[bytes, dict]:
    """(raw body, headers) for a webhook POST: the signature is over exactly these bytes, and ``token`` is unique per call."""
    body = body_bytes(payload)
    return body, {"x-prembly-signature": sign(body, key), "token": token or uuid.uuid4().hex,
                  "content-type": "application/json"}


# --- the API ---------------------------------------------------------------------------------
class FakePrembly:
    """Records every request. ``script_session`` registers a widget session; ``outage`` / ``wallet_empty`` / ``rate_limited`` /
    ``server_error`` / ``timeout`` / ``unauthorised`` make every call fail that way until ``recover``. ``script(...)`` queues
    one-off responses (an ``httpx.Response`` or an exception to raise) consumed before the normal logic."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.fail: Optional[str] = None
        self._scripted: deque = deque()

    # --- arranging --------------------------------------------------------------------------
    def script(self, *items) -> None:
        self._scripted.extend(items)

    def script_session(self, session_id: Optional[str] = None, *, overall_result: str = "passed", status: str = "completed",
                       full_name: str = "Test Person", email: str = "placeholder@example.com", phone: str = "",
                       widget_name: str = "Placeholder Widget", failure_reason: str = "", total_cost: str = "300.00",
                       currency: str = "NGN", is_used: bool = False, face_confidence: Optional[float] = None,
                       liveness_confidence: Optional[float] = None, id_number: Optional[str] = None,
                       payload: Optional[dict] = None) -> dict:
        """Register a session and return it. ``face_confidence`` / ``liveness_confidence`` / ``id_number`` build a ``payload``
        (served only with ``include_payload=true``); its shape follows the webhook sample and is UNCONFIRMED for the pull."""
        session_id = session_id or f"sdk_session_{uuid.uuid4().hex}"
        body: dict[str, Any] = {
            "id": str(uuid.uuid4()), "session_id": session_id, "status": status, "full_name": full_name,
            "end_user_email": email, "end_user_phone": phone, "widget_name": widget_name, "overall_result": overall_result,
            "failure_reason": failure_reason, "total_cost": total_cost, "currency": currency,
            "started_at": "2026-10-27 10:00:00", "completed_at": "2026-10-27 10:01:30", "duration": 90,
            "created_at": "2026-10-27 10:00:00", "is_used": is_used,
        }
        if payload is None and (face_confidence is not None or liveness_confidence is not None or id_number is not None):
            payload = {"face_data": {"status": "success", "response_code": "00", "confidence": face_confidence},
                       "biometric_results": {"liveness_confidence": liveness_confidence},
                       "verification_response": {"number": id_number}}
        if payload is not None:
            body["_payload"] = payload  # kept apart: only served on request
        self.sessions[session_id] = body
        return body

    def outage(self) -> None:
        self.fail = "outage"

    def wallet_empty(self, *, http_402: bool = False) -> None:
        self.fail = "wallet_empty_402" if http_402 else "wallet_empty"

    def rate_limited(self) -> None:
        self.fail = "rate_limited"

    def server_error(self) -> None:
        self.fail = "server_error"

    def timeout(self) -> None:
        self.fail = "timeout"

    def unauthorised(self) -> None:
        self.fail = "unauthorised"

    def recover(self) -> None:
        self.fail = None

    # --- serving ----------------------------------------------------------------------------
    def handle(self, method: str, path: str, headers: dict, body: bytes = b"", query: Optional[dict] = None) -> tuple[int, dict]:
        """Pure core: (status, json body)."""
        query = query or {}
        if headers.get("x-api-key") != TEST_SECRET_KEY or headers.get("x-organisation-id") != TEST_ORG_ID:
            return 401, {"status": False, "message": "Invalid credentials", "errors": None}
        failure = self._failure()
        if failure is not None:
            return failure
        if method != "GET" or not path.startswith(SESSIONS_PATH):
            return 404, {"status": False, "message": "not found", "errors": None}
        tail = path[len(SESSIONS_PATH):].strip("/")
        if tail == "undelivered":
            return 200, self._undelivered(query)
        session = self.sessions.get(tail)
        if session is None:
            return 200, {"status": False, "response_code": "01", "message": "Session not found", "errors": None}
        return 200, self._wrap([self._public(session, query.get("include_payload") == "true")], page=1, page_size=1)

    def _failure(self) -> Optional[tuple[int, dict]]:
        if self.fail == "outage":
            return 200, {"status": False, "response_code": "02", "message": "Service temporarily unavailable"}
        if self.fail == "wallet_empty":
            return 200, {"status": False, "response_code": "03", "message": "Insufficient wallet balance"}
        if self.fail == "wallet_empty_402":
            return 402, {"status": False, "message": "Insufficient wallet balance"}
        if self.fail == "rate_limited":
            return 429, {"status": False, "message": "Too many requests. Try again in 60 seconds."}
        if self.fail == "server_error":
            return 500, {"status": False, "message": "Internal server error"}
        if self.fail == "unauthorised":
            return 401, {"status": False, "message": "Invalid credentials"}
        return None

    def transport_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._scripted:
            item = self._scripted.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        if self.fail == "timeout":
            raise httpx.ReadTimeout("fake prembly timed out", request=request)
        status, payload = self.handle(request.method, request.url.path, {k.lower(): v for k, v in request.headers.items()},
                                      request.content, dict(request.url.params))
        headers = {"Retry-After": "60"} if status == 429 else {}
        return httpx.Response(status, json=payload, headers=headers)

    # --- bodies -----------------------------------------------------------------------------
    @staticmethod
    def _public(session: dict, include_payload: bool) -> dict:
        shown = {k: v for k, v in session.items() if k != "_payload"}
        if include_payload and "_payload" in session:
            shown["payload"] = session["_payload"]
        return shown

    def _undelivered(self, query: dict) -> dict:
        page, size = int(query.get("page", 1)), int(query.get("page_size", 20))
        pending = [s for s in self.sessions.values() if not s["is_used"]]
        window = pending[(page - 1) * size: page * size]
        return self._wrap([self._public(s, False) for s in window], page=page, page_size=size, total=len(pending))

    @staticmethod
    def _wrap(sessions: list, *, page: int, page_size: int, total: Optional[int] = None) -> dict:
        total = len(sessions) if total is None else total
        return {"status": True, "message": "Sessions retrieved successfully",
                "data": {"sessions": sessions,
                         "pagination": {"total": total, "page": page, "page_size": page_size,
                                        "total_pages": max(1, -(-total // page_size))}},
                "errors": None, "meta": {"timestamp": datetime.utcnow().isoformat(), "version": "v1"}}


@pytest.fixture
def fake_prembly(monkeypatch):
    from app.services import prembly

    fake = FakePrembly()
    monkeypatch.setattr(prembly, "_transport", httpx.MockTransport(fake.transport_handler))
    for name, value in {
        "PREMBLY_BASE_URL": TEST_BASE_URL, "PREMBLY_SECRET_KEY": TEST_SECRET_KEY, "PREMBLY_ORG_ID": TEST_ORG_ID,
        "PREMBLY_WEBHOOK_KEY": TEST_WEBHOOK_KEY, "KYC_FINGERPRINT_KEY": TEST_FINGERPRINT_KEY,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return fake
