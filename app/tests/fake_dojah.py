"""A fake Dojah KYC-widget verification API (caregiver KYC), plus helpers to sign webhooks the way Dojah documents.

Nothing here touches the network. ``FakeDojah.handle`` is plain request-in, response-out logic; tests reach it through an
``httpx.MockTransport`` (the ``fake_dojah`` fixture), like ``fake_meta``. The shapes come from Dojah's public docs and from the
Timp / Weysure parsers (docs/plans/2026-10-30-caregiver-kyc-dojah-delta.md): every field marked UNCONFIRMED in ``kyc_seam`` (D1-D14)
is a guess here until the K8 live pass, so update this file from the real captures.

Followed:
  pull            ``GET /api/v1/kyc/verification?reference_id=...`` with ``Authorization: <secret key>`` and ``AppId``; the answer is
                  ``{"entity": {reference_id, verification_status, verification_type, data: {government_data, selfie}}}``
  signature       ``x-dojah-signature`` = hex HMAC-SHA256 of the raw body, keyed by the webhook secret
  webhook body    top-level ``reference_id``, ``verification_status``, ``status``, ``verification_type``, ``data``, ``metadata``
Decoys: every answer and webhook also carries image links and a photo field with obvious sentinel strings, so the privacy tests can
prove nothing of the kind is read, stored or logged (``SENTINEL_*``).

All ids, names and numbers below are obviously fake placeholders.
"""
import hashlib
import hmac
import json
import uuid
from collections import deque
from typing import Any, Optional

import httpx
import pytest

from app.core.config import settings

TEST_BASE_URL = "http://dojah.test"
TEST_APP_ID = "test-dojah-app-id-placeholder"
TEST_PUBLIC_KEY = "test-dojah-public-key-placeholder"
TEST_SECRET_KEY = "test-dojah-secret-key-placeholder"
TEST_WIDGET_ID = "test-dojah-widget-id-placeholder"
TEST_WEBHOOK_SECRET = "test-dojah-webhook-secret-placeholder"
TEST_FINGERPRINT_KEY = "test-kyc-fingerprint-key-placeholder"
# The three keys a production Settings needs (the app refuses to boot without them): for tests that build a production Settings.
PRODUCTION_KYC_KEYS = {"DOJAH_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET, "DOJAH_SECRET_KEY": TEST_SECRET_KEY,
                       "KYC_FINGERPRINT_KEY": TEST_FINGERPRINT_KEY}
SANDBOX_NIN = "70000000017"  # a placeholder, not a real NIN
SANDBOX_BVN = "22200000017"  # a placeholder, not a real BVN (Weysure's)
SENTINEL_PHOTO = "data:image/jpeg;base64,SENTINEL-PHOTO-do-not-store"
SENTINEL_URLS = {"selfie_url": "https://images.dojah.invalid/SENTINEL-selfie.jpg", "id_url": "https://images.dojah.invalid/SENTINEL-id.jpg",
                 "back_url": "https://images.dojah.invalid/SENTINEL-back.jpg", "verification_pdf": "https://images.dojah.invalid/SENTINEL.pdf"}
VERIFICATION_PATH = "/api/v1/kyc/verification"


def new_reference() -> str:
    """A reference shaped like the ones the server mints (``kyc_seam.mint_reference``)."""
    return "OKL-" + uuid.uuid4().hex


# --- webhook builders ------------------------------------------------------------------------
def sign(body: bytes, secret: str) -> str:
    """``x-dojah-signature``: lower-case hex HMAC-SHA256 over the raw body."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook_payload(reference_id: str, *, status: str = "Completed", entity: Optional[dict] = None, **extra: Any) -> dict:
    """A webhook body: the verification's fields at the TOP level (no ``entity`` wrapper), plus the decoy image links. ``entity``
    (from ``FakeDojah.entity``) supplies ``data`` and ``verification_type``; ``extra`` overrides or adds top-level keys."""
    body: dict[str, Any] = {"reference_id": reference_id, "widget_id": TEST_WIDGET_ID, "verification_status": status,
                            "status": status == "Completed", "metadata": {}, **SENTINEL_URLS}
    if entity is not None:
        body["verification_type"] = entity.get("verification_type")
        body["data"] = entity.get("data")
    body.update(extra)
    return body


def body_bytes(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def signed_webhook(payload: dict, secret: str = TEST_WEBHOOK_SECRET) -> tuple[bytes, dict]:
    """(raw body, headers) for a webhook POST: the signature is over exactly these bytes."""
    body = body_bytes(payload)
    return body, {"x-dojah-signature": sign(body, secret), "content-type": "application/json"}


# --- the API ---------------------------------------------------------------------------------
class FakeDojah:
    """Records every request. ``script_verification`` registers a verification under a reference (an unregistered reference is a
    404, as an unknown one probably is); ``outage`` / ``wallet_empty`` / ``rate_limited`` / ``server_error`` / ``timeout`` /
    ``unauthorised`` make every call fail that way until ``recover``. ``script(...)`` queues one-off responses (an
    ``httpx.Response`` or an exception to raise) consumed before the normal logic."""

    def __init__(self):
        self.verifications: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.fail: Optional[str] = None
        self._scripted: deque = deque()

    # --- arranging --------------------------------------------------------------------------
    def script(self, *items) -> None:
        self._scripted.extend(items)

    @staticmethod
    def entity(reference_id: str, *, status: str = "Completed", id_type: Optional[str] = "nin", id_number: Optional[str] = SANDBOX_NIN,
               first_name: Optional[str] = "Test", middle_name: Optional[str] = None, last_name: Optional[str] = "Person",
               date_of_birth: Optional[str] = "12-Apr-1990", government: Optional[bool] = True, selfie: Optional[bool] = True,
               confidence: Optional[float] = 92.0, message: Optional[str] = None, verification_type: Optional[str] = "default",
               data: Optional[dict] = None, **extra: Any) -> dict:
        """The ``entity`` of a pull (and, minus the wrapper, the shape of a webhook). ``None`` leaves a field out; ``data`` replaces
        the whole ``data`` block. The registry record carries a decoy photo."""
        if data is None:
            person: dict[str, Any] = {"image": SENTINEL_PHOTO, "photo": SENTINEL_PHOTO}
            for key, value in (("first_name", first_name), ("middle_name", middle_name), ("last_name", last_name),
                               ("date_of_birth", date_of_birth)):
                if value is not None:
                    person[key] = value
            if id_type and id_number is not None:
                person[id_type] = id_number
            data = {}
            if government is not None:
                data["government_data"] = {"status": government, "data": {id_type: {"entity": person}} if id_type else {}}
                if message and not government:
                    data["government_data"]["message"] = message
            if selfie is not None:
                data["selfie"] = {"status": selfie}
                if confidence is not None:
                    data["selfie"]["confidence_value"] = confidence
                if message and not selfie:
                    data["selfie"]["message"] = message
        entity: dict[str, Any] = {"reference_id": reference_id, "verification_status": status, "data": data, **SENTINEL_URLS}
        if verification_type == "default":
            verification_type = id_type.upper() if id_type else None
        if verification_type is not None:
            entity["verification_type"] = verification_type
        entity.update(extra)
        return entity

    def script_verification(self, reference_id: str, **fields: Any) -> dict:
        """Register a verification and return its entity. ``fields`` are those of ``entity``."""
        entity = self.entity(reference_id, **fields)
        self.verifications[reference_id] = entity
        return entity

    def script_clean_pass(self, reference_id: str, caregiver, **overrides: Any) -> dict:
        """A verification that passes every K4 check for ``caregiver`` (the registry name and date of birth are the caregiver's own,
        the face confidence is high, the number is a placeholder NIN). ``overrides`` replace any field of ``entity``."""
        parts = (caregiver.full_name or "").split()
        fields: dict[str, Any] = dict(
            status="Completed", id_type="nin", id_number=SANDBOX_NIN, confidence=92.0, government=True, selfie=True,
            first_name=parts[0] if parts else None, middle_name=" ".join(parts[1:-1]) or None,
            last_name=parts[-1] if len(parts) > 1 else None,
            date_of_birth=caregiver.date_of_birth.strftime("%d-%b-%Y") if caregiver.date_of_birth else None)
        fields.update(overrides)
        return self.script_verification(reference_id, **fields)

    def outage(self) -> None:
        self.fail = "outage"

    def wallet_empty(self, *, http_400: bool = False) -> None:
        self.fail = "wallet_empty_400" if http_400 else "wallet_empty"

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
    def handle(self, method: str, path: str, headers: dict, query: Optional[dict] = None) -> tuple[int, dict]:
        """Pure core: (status, json body)."""
        query = query or {}
        if headers.get("authorization") != TEST_SECRET_KEY or headers.get("appid") != TEST_APP_ID:
            return 401, {"error": "Unauthorized"}
        failure = self._failure()
        if failure is not None:
            return failure
        if method != "GET" or path != VERIFICATION_PATH:
            return 404, {"error": "not found"}
        entity = self.verifications.get(query.get("reference_id", ""))
        if entity is None:
            return 404, {"error": "Verification not found"}
        return 200, {"entity": entity}

    def _failure(self) -> Optional[tuple[int, dict]]:
        if self.fail == "outage":
            return 503, {"error": "Service unavailable"}
        if self.fail == "wallet_empty":
            return 402, {"error": "Insufficient wallet balance"}
        if self.fail == "wallet_empty_400":
            return 400, {"error": "Insufficient balance, please fund your wallet"}
        if self.fail == "rate_limited":
            return 429, {"error": "Too many requests"}
        if self.fail == "server_error":
            return 500, {"error": "Internal server error"}
        if self.fail == "unauthorised":
            return 401, {"error": "Unauthorized"}
        return None

    def transport_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._scripted:
            item = self._scripted.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        if self.fail == "timeout":
            raise httpx.ReadTimeout("fake dojah timed out", request=request)
        status, payload = self.handle(request.method, request.url.path, {k.lower(): v for k, v in request.headers.items()},
                                      dict(request.url.params))
        headers = {"Retry-After": "60"} if status == 429 else {}
        return httpx.Response(status, json=payload, headers=headers)

    @property
    def references_pulled(self) -> list[str]:
        return [r.url.params.get("reference_id") for r in self.requests]


@pytest.fixture
def fake_dojah(monkeypatch):
    from app.services import dojah

    fake = FakeDojah()
    monkeypatch.setattr(dojah, "_transport", httpx.MockTransport(fake.transport_handler))
    monkeypatch.setattr(dojah, "_sleep", lambda seconds: None)  # the client's back-off between attempts
    for name, value in {
        "DOJAH_BASE_URL": TEST_BASE_URL, "DOJAH_APP_ID": TEST_APP_ID, "DOJAH_PUBLIC_KEY": TEST_PUBLIC_KEY,
        "DOJAH_SECRET_KEY": TEST_SECRET_KEY, "DOJAH_WIDGET_ID": TEST_WIDGET_ID, "DOJAH_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
        "KYC_FINGERPRINT_KEY": TEST_FINGERPRINT_KEY,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return fake


# --- the provider-availability flag (K2; the real one is Redis, K4 writes it) ---------------------------------------
class FakeProviderAvailability:
    def __init__(self) -> None:
        self.available = True
        self.errors = 0
        self.alerts: list[str] = []  # the alert kinds that were let through the throttle
        self._claimed: set[str] = set()

    def is_available(self) -> bool:
        return self.available

    def mark_unavailable(self, ttl_seconds: int = 900) -> None:
        self.available = False

    def mark_available(self) -> None:
        self.available = True

    def record_error(self) -> bool:
        self.errors += 1
        if self.errors >= 3:
            self.mark_unavailable()
            return True
        return False

    def record_success(self) -> None:
        self.errors = 0
        self.available = True

    def claim_alert(self, kind: str, window_seconds: int = 3600) -> bool:
        if kind in self._claimed:
            return False
        self._claimed.add(kind)
        self.alerts.append(kind)
        return True


@pytest.fixture
def kyc_availability():
    from app.main import app
    from app.services.kyc_availability import get_provider_availability

    fake = FakeProviderAvailability()
    app.dependency_overrides[get_provider_availability] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_provider_availability, None)


# --- the ARQ queue the K3 webhook enqueues on (the real one is Redis) --------------------------------------------------
class FakeKycEventQueue:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.fail: Optional[Exception] = None

    async def enqueue(self, job: dict) -> None:
        from app.services.kyc_queue import QueueRefused

        if self.fail is not None:
            raise self.fail
        if any(j["event_id"] == job["event_id"] for j in self.jobs):  # ARQ refuses a second job with the same _job_id
            raise QueueRefused("already queued")
        self.jobs.append(job)


@pytest.fixture
def kyc_queue():
    from app.main import app
    from app.services.kyc_queue import get_kyc_event_queue

    queue = FakeKycEventQueue()
    app.dependency_overrides[get_kyc_event_queue] = lambda: queue
    try:
        yield queue
    finally:
        app.dependency_overrides.pop(get_kyc_event_queue, None)
