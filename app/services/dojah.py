"""A thin httpx client for Dojah's KYC-widget verification API (caregiver KYC), the only place Oakly talks to Dojah.

One call: ``get_verification(reference_id)`` = ``GET {base}/api/v1/kyc/verification?reference_id=...`` with the SECRET key in
``Authorization`` (no ``Bearer``) and the app id in ``AppId``. The server mints ``reference_id`` (``OKL-<uuid4 hex>``) before the
widget is launched, so the same string is the widget's reference, the webhook's ``reference_id`` and the pull key. Ported from
Weysure's ``app/integrations/dojah/client.py`` (sync httpx, 10 s timeout, 3 attempts, only a 5xx or a transport error is retried:
a 4xx fails the same way every time) and adapted to Oakly's error kinds, which ``kyc_hooks`` / ``kyc_reconcile`` already use.

Sources (docs/plans/2026-10-30-caregiver-kyc-dojah-delta.md): [DOCS] Dojah's public pages, [PORT] working code in Timpbills and
Weysure, [UNVERIFIED] nothing of Oakly has met the real service until the K8 live pass. The UNCONFIRMED list is in
``kyc_seam`` (D1-D14) and in the KD1 SOP.

Error mapping: 402 wallet empty (fund it), 401/403 key or app id refused, 404 reference unknown (the widget was never finished, or
not yet), 429 rate limited, 5xx unavailable, other 4xx rejected; a 400 whose message talks about balance is a wallet problem too
[UNVERIFIED: how Dojah reports an empty wallet]. A timeout or connection failure is its own kind. A call that exhausted its retries
raises the last failure; ``DojahError.retryable`` tells callers (the ARQ job, the reconcile cron) whether to ask again later.

Privacy: the answer carries the registry record (name, date of birth, ID number, a photo) and image links. ``_extract`` copies out
the handful of fields the decision needs, from a WHITELIST (``REGISTRY_FIELDS``): the photo, the image URLs (``selfie_url``,
``id_url``, ``back_url``, ``verification_pdf``) and everything else are never read into the returned object. The registry fields
sit in memory only (hidden from ``repr``). Nothing here logs a response body, and the secret key never appears in an error.

Tests set ``_transport`` to an ``httpx.MockTransport`` (see ``app/tests/fake_dojah.py``) and ``_sleep`` to a no-op; without a
transport the suite's network guard blocks the call.
"""
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs every request URL at INFO; ours carries the reference

# Test hooks only. None / time.sleep in production.
_transport: Optional[httpx.BaseTransport] = None
_sleep: Callable[[float], None] = time.sleep

VERIFICATION_PATH = "/api/v1/kyc/verification"  # [DOCS][PORT]
DEFAULT_TIMEOUT_SECONDS = 10.0
ATTEMPTS = 3
BACKOFF_SECONDS = 0.5
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 300.0
REFERENCE_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"  # it goes into a query string; ours are ``OKL-`` + 32 hex
_REFERENCE_ID = re.compile(REFERENCE_ID_PATTERN)
ID_TYPES = ("bvn", "nin")  # the two registries the widget flow is built for (delta section 2, U12/U13)
# The ONLY registry fields ever copied out of an answer ([PORT] Weysure parse.py). The id number sits under the id type's own key.
REGISTRY_FIELDS = ("first_name", "middle_name", "last_name", "date_of_birth")
MAX_MESSAGE_CHARS = 300

# Error kinds
NOT_CONFIGURED = "not_configured"  # our secret key or app id is empty: nothing was sent
UNAUTHORISED = "unauthorised"  # 401/403: Dojah refused our key or app id
NOT_FOUND = "not_found"  # 404: no verification under this reference (yet)
UNAVAILABLE = "unavailable"  # 5xx
WALLET_EMPTY = "wallet_empty"  # 402 (or a 400 that says so): every check fails until the wallet is funded
RATE_LIMITED = "rate_limited"  # 429
REJECTED = "rejected"  # other 4xx
BAD_RESPONSE = "bad_response"  # not JSON
REFERENCE_MISMATCH = "reference_mismatch"  # an answer that does not name the reference we asked for: final for that call, no retry, not an outage
TIMEOUT = "timeout"
NETWORK = "network"

RETRYABLE_KINDS = frozenset({UNAVAILABLE, RATE_LIMITED, TIMEOUT, NETWORK, BAD_RESPONSE})
# The caregiver is told "verification is temporarily unavailable" for these (design 7.3).
OUTAGE_KINDS = frozenset({UNAVAILABLE, WALLET_EMPTY, RATE_LIMITED, TIMEOUT, NETWORK, BAD_RESPONSE, UNAUTHORISED, NOT_CONFIGURED})
# These also page a person: they break every caregiver at once and retrying cannot fix them.
OPS_ALERT_KINDS = frozenset({WALLET_EMPTY, UNAUTHORISED, NOT_CONFIGURED})

_WALLET_WORDS = ("insufficient", "balance", "wallet", "fund")  # [UNVERIFIED] how Dojah words an empty wallet


class DojahError(Exception):
    """A Dojah call that did not give a usable answer. ``str(exc)`` carries no response body and no key."""

    def __init__(self, kind: str, *, http_status: Optional[int] = None, retry_after: Optional[float] = None):
        self.kind = kind
        self.http_status = http_status
        self.retry_after = retry_after  # seconds; set for rate_limited
        super().__init__(f"dojah {kind} (http={http_status})")

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @property
    def outage(self) -> bool:
        return self.kind in OUTAGE_KINDS

    @property
    def needs_ops_alert(self) -> bool:
        return self.kind in OPS_ALERT_KINDS


def classify(http_status: int, message: str = "") -> str:
    """The error kind for an answer with a failure status. ``message`` is only searched, never kept."""
    if http_status == 402:
        return WALLET_EMPTY
    if http_status == 429:
        return RATE_LIMITED
    if http_status in (401, 403):
        return UNAUTHORISED
    if http_status == 404:
        return NOT_FOUND
    if http_status >= 500:
        return UNAVAILABLE
    if any(word in message.casefold() for word in _WALLET_WORDS):
        return WALLET_EMPTY
    return REJECTED


@dataclass(frozen=True)
class DojahVerification:
    """What Dojah says about one widget run, reduced to what the decision needs. ``registry`` (the BVN/NIN holder's name, date of
    birth and number) and ``message`` are in memory only: hidden from ``repr``, never stored, never logged."""
    reference_id: str
    verification_status: Optional[str] = None  # Ongoing | Pending | Completed | Failed | Abandoned (as Dojah spells it)
    verification_type: Optional[str] = None  # "BVN" / "NIN"
    government_status: Optional[bool] = None  # ``data.government_data.status``
    selfie_status: Optional[bool] = None  # ``data.selfie.status``: liveness AND face match, as one flag [UNVERIFIED]
    selfie_confidence: Optional[float] = None  # ``data.selfie.confidence_value``, 0-100 [UNVERIFIED field name; the sandbox does not send it]
    selfie_confidence_given: bool = False  # the field is present and not null (even if unusable): only its ABSENCE lets the flag stand alone
    registry_type: Optional[str] = None  # bvn | nin
    registry: dict = field(default_factory=dict, repr=False)
    message: Optional[str] = field(default=None, repr=False)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _confidence(selfie: dict) -> Optional[float]:
    value = selfie.get("confidence_value")  # field name unverified until the sandbox run (Weysure marks it so too)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None  # json accepts Infinity and NaN
    return float(value)


def _id_type(entity: dict, records: dict) -> Optional[str]:
    declared = str(entity.get("verification_type") or "").lower()
    if declared in ID_TYPES:
        return declared
    found = [t for t in ID_TYPES if t in records]
    return found[0] if len(found) == 1 else None


def _messages(*blocks: dict) -> Optional[str]:
    """The free text of the blocks that did NOT succeed. A passing block's message ("Successfully validated your liveness", seen in the
    live sandbox) must never be read as the reason for another block's failure."""
    parts = [m for m in (_text(b.get("message")) for b in blocks if b.get("status") is not True) if m]
    return " | ".join(parts)[:MAX_MESSAGE_CHARS] or None


def _extract(body: Any, reference_id: str) -> DojahVerification:
    """The one verification in an answer: ``{"entity": {...}}`` (or the entity itself). An answer about some other reference, or
    with none, is never used (the binding must not rest on a body that does not name our reference)."""
    entity = _dict(_dict(body).get("entity")) or _dict(body)
    if _text(entity.get("reference_id")) != reference_id:
        raise DojahError(REFERENCE_MISMATCH)  # not BAD_RESPONSE: that kind is an outage and would stall the cron behind this attempt
    data = _dict(entity.get("data"))
    government = _dict(data.get("government_data"))
    selfie = _dict(data.get("selfie"))
    records = _dict(government.get("data"))
    id_type = _id_type(entity, records)
    person = _dict(_dict(records.get(id_type)).get("entity")) if id_type else {}
    registry = {key: person[key] for key in (id_type, *REGISTRY_FIELDS) if key and key in person}
    declared = _text(entity.get("verification_type"))
    return DojahVerification(
        reference_id=reference_id, verification_status=_text(entity.get("verification_status")),
        verification_type=declared[:16] if declared else None, government_status=_bool(government.get("status")),
        selfie_status=_bool(selfie.get("status")), selfie_confidence=_confidence(selfie),
        selfie_confidence_given=selfie.get("confidence_value") is not None, registry_type=id_type,
        registry=registry, message=_messages(entity, government, selfie))


def _retry_after(response: httpx.Response) -> float:
    try:
        wait = float(response.headers.get("retry-after", ""))
    except ValueError:
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS
    return min(max(wait, 0.0), MAX_RETRY_AFTER_SECONDS)


def _error_message(response: httpx.Response) -> str:
    """The failure text of a 4xx answer, for classification only (a 400 about the wallet)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    body = _dict(body)
    return " ".join(str(body.get(key)) for key in ("message", "error", "detail") if isinstance(body.get(key), str))


class DojahClient:
    def __init__(self, *, base_url: str, secret_key: str, app_id: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 attempts: int = ATTEMPTS, transport: Optional[httpx.BaseTransport] = None):
        self._base_url = base_url.rstrip("/")
        self._secret_key = secret_key
        self._app_id = app_id
        self._timeout = timeout
        self._attempts = max(1, attempts)
        self._transport = transport

    @classmethod
    def from_settings(cls) -> "DojahClient":
        return cls(base_url=settings.DOJAH_BASE_URL, secret_key=settings.DOJAH_SECRET_KEY, app_id=settings.DOJAH_APP_ID)

    def get_verification(self, reference_id: str) -> DojahVerification:
        """The authoritative record of one widget run (design K-C1: the pull, made with the SECRET key). Raises ``ValueError`` for a
        reference we may not put in a query string, ``DojahError`` for everything Dojah-side."""
        if not _REFERENCE_ID.fullmatch(reference_id or ""):
            raise ValueError("invalid dojah reference id")
        if not self._secret_key or not self._app_id:
            raise DojahError(NOT_CONFIGURED)
        response = self._get(reference_id)
        try:
            body = response.json()
        except ValueError:
            raise DojahError(BAD_RESPONSE, http_status=response.status_code) from None
        return _extract(body, reference_id)

    def _get(self, reference_id: str) -> httpx.Response:
        headers = {"Authorization": self._secret_key, "AppId": self._app_id, "Accept": "application/json"}
        failure = DojahError(NETWORK)
        with httpx.Client(timeout=self._timeout, transport=self._transport or _transport) as http:
            for attempt in range(1, self._attempts + 1):
                try:
                    response = http.get(self._base_url + VERIFICATION_PATH, params={"reference_id": reference_id}, headers=headers)
                except httpx.TimeoutException:
                    failure = DojahError(TIMEOUT)
                    logger.warning("dojah: timed out (attempt %d of %d)", attempt, self._attempts)
                except httpx.HTTPError as exc:
                    failure = DojahError(NETWORK)
                    logger.warning("dojah: transport error %s (attempt %d of %d)", type(exc).__name__, attempt, self._attempts)
                else:
                    if response.status_code < 400:
                        return response
                    kind = classify(response.status_code, _error_message(response) if response.status_code < 500 else "")
                    failure = DojahError(kind, http_status=response.status_code,
                                         retry_after=_retry_after(response) if kind == RATE_LIMITED else None)
                    logger.warning("dojah: %s (attempt %d of %d)", failure, attempt, self._attempts)
                    if response.status_code < 500:
                        raise failure  # a 4xx fails the same way every time
                if attempt < self._attempts:
                    _sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
        raise failure
