"""A thin httpx client for the WhatsApp Cloud API (Graph API ``/{phone-number-id}/messages``).

It sends and reports; it never touches the database (``outbound.send_and_record`` writes the
metadata row) and never raises for a delivery problem: a failed send is a ``SendResult`` with
``ok=False`` and Meta's error code, so a conversation is never lost to an exception half way through.

Retry policy
- 429 and 5xx: retried with exponential backoff, honouring ``Retry-After`` when Meta sends it.
- Connection errors (refused, connect timeout): retried, because nothing reached Meta.
- A read/write timeout is NOT retried. Meta may already have accepted the message, the Cloud API has
  no idempotency key, and a retry would send the patient the same message twice.
- Any other 4xx (131026 undeliverable, 131047 re-engagement required, 190 bad token...) is terminal.

Tests set ``_transport`` to an ``httpx.MockTransport`` (see ``app/tests/fake_meta.py``) and ``_sleep``
to a no-op; without a transport the suite's network guard blocks the call.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.channels.whatsapp.messages import OutboundMessage, to_payload
from app.core.config import settings
from app.core.phone import mask_phone

logger = logging.getLogger(__name__)

# Test hooks only. None / time.sleep in production.
_transport: Optional[httpx.BaseTransport] = None
_sleep = time.sleep

MAX_RETRY_AFTER_SECONDS = 10.0  # never stall a worker slot for longer than this on Meta's say-so


@dataclass(frozen=True)
class SendResult:
    ok: bool
    meta_message_id: Optional[str] = None
    error_code: Optional[str] = None  # Meta's code, or "timeout" / "network" / "http_<status>"
    status_code: Optional[int] = None
    attempts: int = 1


def _error_code(response: httpx.Response) -> str:
    try:
        code = response.json().get("error", {}).get("code")
        if code is not None:
            return str(code)
    except ValueError:
        pass
    return f"http_{response.status_code}"


class MetaCloudClient:
    def __init__(self, *, base_url: str, version: str, phone_number_id: str, access_token: str,
                 timeout: float, max_attempts: int, backoff_seconds: float):
        self._url = f"{base_url.rstrip('/')}/{version}/{phone_number_id}/messages"
        self._token = access_token
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff_seconds

    @classmethod
    def from_settings(cls) -> "MetaCloudClient":
        return cls(
            base_url=settings.WHATSAPP_GRAPH_URL,
            version=settings.WHATSAPP_GRAPH_VERSION,
            phone_number_id=settings.WHATSAPP_PHONE_NUMBER_ID,
            access_token=settings.WHATSAPP_ACCESS_TOKEN,
            timeout=settings.WHATSAPP_HTTP_TIMEOUT_SECONDS,
            max_attempts=settings.WHATSAPP_MAX_SEND_ATTEMPTS,
            backoff_seconds=settings.WHATSAPP_RETRY_BACKOFF_SECONDS,
        )

    def send(self, to: str, message: OutboundMessage) -> SendResult:
        """Send one message. ``to`` is the recipient's wa_id (digits, no plus)."""
        payload = to_payload(to, message)
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        who = mask_phone(to)
        last = SendResult(ok=False, error_code="network", attempts=0)

        with httpx.Client(timeout=self._timeout, transport=_transport) as http:
            for attempt in range(1, self._max_attempts + 1):
                delay = self._backoff * (2 ** (attempt - 1))
                try:
                    response = http.post(self._url, json=payload, headers=headers)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    last = SendResult(ok=False, error_code="network", attempts=attempt)
                    logger.warning("whatsapp send to %s: connection failed (%s), attempt %d",
                                   who, type(exc).__name__, attempt)
                except httpx.TimeoutException:
                    logger.warning("whatsapp send to %s: timed out waiting for Meta; not retrying (it may "
                                   "have been accepted)", who)
                    return SendResult(ok=False, error_code="timeout", attempts=attempt)
                except httpx.HTTPError as exc:
                    logger.warning("whatsapp send to %s: transport error (%s)", who, type(exc).__name__)
                    return SendResult(ok=False, error_code="network", attempts=attempt)
                else:
                    if response.status_code < 300:
                        message_id = self._message_id(response)
                        if message_id is None:
                            return SendResult(ok=False, error_code="bad_response",
                                              status_code=response.status_code, attempts=attempt)
                        return SendResult(ok=True, meta_message_id=message_id,
                                          status_code=response.status_code, attempts=attempt)
                    last = SendResult(ok=False, error_code=_error_code(response),
                                      status_code=response.status_code, attempts=attempt)
                    if response.status_code != 429 and response.status_code < 500:
                        logger.warning("whatsapp send to %s rejected: %s", who, last.error_code)
                        return last
                    logger.warning("whatsapp send to %s: HTTP %d (code %s), attempt %d",
                                   who, response.status_code, last.error_code, attempt)
                    delay = self._retry_after(response, delay)

                if attempt < self._max_attempts:
                    _sleep(delay)
        return last

    @staticmethod
    def _message_id(response: httpx.Response) -> Optional[str]:
        try:
            return response.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _retry_after(response: httpx.Response, default: float) -> float:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return min(max(float(raw), 0.0), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
        return default
