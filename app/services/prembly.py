"""A thin httpx client for Prembly's widget-session API (caregiver KYC), the only place Oakly talks to Prembly.

K1 scope: read a session (``get_session``) and list the sessions whose webhook was not delivered (``list_undelivered``), with
Prembly's error conventions mapped onto one exception. It sends nothing about a person, stores nothing and never touches the
database. The decision engine (K4) and the webhook job (K3) build on it.

Error mapping (docs/plans/2026-10-27-caregiver-kyc-design.md 3.2). Prembly answers HTTP 200 for some failures, so the body's
``response_code`` is read first: ``01`` record not found, ``02`` service unavailable (retry with back-off), ``03`` wallet empty,
``07`` BVN blocked or watch-listed (never retry). Then the HTTP status: 402 wallet empty (fund it), 429 rate limited (the docs'
example waits 60 s), 401/403 bad key, 404 not found, other 4xx rejected, 5xx unavailable. A timeout or connection failure is its
own kind. The client makes ONE attempt per call; callers decide whether to retry (``PremblyError.retryable``) and how to back off.

UNCONFIRMED against real Prembly (design doc section 15, Q3): the exact session-retrieval path and parameters (the two public
reference pages contradict each other), the shape of the by-id response, the pagination arithmetic, whether ``payload`` is
returned, and whether the sandbox has its own host. The paths and the parsing sit behind ``SESSION_PATH``, ``UNDELIVERED_PATH``,
``_extract_session`` and ``_extract_sessions`` so the K0 spike can change them in one place. Until K8 nothing here has met the
real service.

Privacy: a session carries the end user's name, email and phone. They are kept off ``repr`` and are never logged; nothing in this
module logs a response body.

Tests set ``_transport`` to an ``httpx.MockTransport`` (see ``app/tests/fake_prembly.py``); without one the suite's network guard
blocks the call.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Test hook only. None in production.
_transport: Optional[httpx.BaseTransport] = None

# UNCONFIRMED (Q3): the API & Webhooks page shows ``.../sessions/{session_id}/``, the reference page ``.../sessions/undelivered/
# {session_id}/?include_payload=true``. The first is used; change it here after the spike.
SESSION_PATH = "/api/v1/checker-widget/sdk/sessions/{session_id}/"
UNDELIVERED_PATH = "/api/v1/checker-widget/sdk/sessions/undelivered/"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60.0  # the 429 example in Prembly's docs suggests waiting a minute
MAX_RETRY_AFTER_SECONDS = 300.0
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")  # the id can come from the client; it goes into a URL path

# Error kinds
NOT_CONFIGURED = "not_configured"  # our key or org id is empty: nothing was sent
UNAUTHORISED = "unauthorised"  # 401/403: Prembly refused our key
NOT_FOUND = "not_found"  # code 01 or HTTP 404
UNAVAILABLE = "unavailable"  # code 02, HTTP 5xx
WALLET_EMPTY = "wallet_empty"  # code 03 or HTTP 402: every check fails until the wallet is funded
WATCHLISTED = "watchlisted"  # code 07: do not retry with the same BVN
RATE_LIMITED = "rate_limited"  # HTTP 429
REJECTED = "rejected"  # other 4xx, or a wrapper that says ``status: false``
BAD_RESPONSE = "bad_response"  # not JSON, or not the shape we read
TIMEOUT = "timeout"
NETWORK = "network"

RETRYABLE_KINDS = frozenset({UNAVAILABLE, RATE_LIMITED, TIMEOUT, NETWORK, BAD_RESPONSE})
# The caregiver is told "verification is temporarily unavailable" for these (design 7.3).
OUTAGE_KINDS = frozenset({UNAVAILABLE, WALLET_EMPTY, RATE_LIMITED, TIMEOUT, NETWORK, BAD_RESPONSE, UNAUTHORISED, NOT_CONFIGURED})
# These also page a person: they break every caregiver at once and retrying cannot fix them.
OPS_ALERT_KINDS = frozenset({WALLET_EMPTY, UNAUTHORISED, NOT_CONFIGURED})

_CODE_KINDS = {"01": NOT_FOUND, "02": UNAVAILABLE, "03": WALLET_EMPTY, "07": WATCHLISTED}


class PremblyError(Exception):
    """A Prembly call that did not give a usable answer. ``str(exc)`` carries no response body."""

    def __init__(self, kind: str, *, http_status: Optional[int] = None, response_code: Optional[str] = None,
                 retry_after: Optional[float] = None):
        self.kind = kind
        self.http_status = http_status
        self.response_code = response_code
        self.retry_after = retry_after  # seconds; set for rate_limited
        super().__init__(f"prembly {kind} (http={http_status}, response_code={response_code})")

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @property
    def outage(self) -> bool:
        return self.kind in OUTAGE_KINDS

    @property
    def needs_ops_alert(self) -> bool:
        return self.kind in OPS_ALERT_KINDS


def classify(http_status: Optional[int], response_code: Optional[str] = None) -> str:
    """The error kind for a failed answer. The body's ``response_code`` wins over the HTTP status when it is one we know."""
    if response_code in _CODE_KINDS:
        return _CODE_KINDS[response_code]
    if http_status is None:
        return BAD_RESPONSE
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
    return REJECTED


@dataclass(frozen=True)
class PremblySession:
    """What Prembly says about one widget session. The person's name, email, phone and any raw payload are in memory only."""
    session_id: str
    status: Optional[str] = None
    overall_result: Optional[str] = None
    failure_reason: Optional[str] = None
    total_cost: Optional[Decimal] = None
    currency: Optional[str] = None
    widget_name: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    is_used: Optional[bool] = None
    full_name: Optional[str] = field(default=None, repr=False)
    end_user_email: Optional[str] = field(default=None, repr=False)
    end_user_phone: Optional[str] = field(default=None, repr=False)
    payload: Optional[dict] = field(default=None, repr=False)  # UNCONFIRMED: the raw ``payload`` if Prembly returns one


@dataclass(frozen=True)
class PremblySessionPage:
    sessions: tuple
    page: int
    page_size: int
    total: Optional[int] = None
    total_pages: Optional[int] = None

    @property
    def has_more(self) -> bool:
        # Not derived from ``total_pages``: the documented sample contradicts itself (10 items at 2 per page, 11 pages). A
        # full page means "ask for the next one"; a short or empty page ends the drain.
        return len(self.sessions) >= self.page_size


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    for parse in (lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"), datetime.fromisoformat):
        try:
            return parse(value).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _parse_cost(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value != "" else None


def _to_session(raw: dict) -> PremblySession:
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise PremblyError(BAD_RESPONSE)
    payload = raw.get("payload")
    is_used = raw.get("is_used")
    return PremblySession(
        session_id=session_id, status=_text(raw.get("status")), overall_result=_text(raw.get("overall_result")),
        failure_reason=_text(raw.get("failure_reason")), total_cost=_parse_cost(raw.get("total_cost")),
        currency=_text(raw.get("currency")), widget_name=_text(raw.get("widget_name")),
        started_at=_parse_time(raw.get("started_at")), completed_at=_parse_time(raw.get("completed_at")),
        is_used=is_used if isinstance(is_used, bool) else None, full_name=_text(raw.get("full_name")),
        end_user_email=_text(raw.get("end_user_email")), end_user_phone=_text(raw.get("end_user_phone")),
        payload=payload if isinstance(payload, dict) else None,
    )


def _extract_session(body: dict, session_id: str) -> PremblySession:
    """The one session in a by-id answer. UNCONFIRMED shape (Q3), so three are accepted: ``data`` is the session, ``data.session``
    is, or ``data.sessions`` is a list (the documented wrapper) from which the requested id is picked."""
    data = body.get("data", body)
    if isinstance(data, dict):
        if isinstance(data.get("session"), dict):
            data = data["session"]
        elif isinstance(data.get("sessions"), list):
            data = next((s for s in data["sessions"] if isinstance(s, dict) and s.get("session_id") == session_id), None)
    if not isinstance(data, dict):
        raise PremblyError(NOT_FOUND if data is None else BAD_RESPONSE)
    session = _to_session(data)
    if session.session_id != session_id:
        raise PremblyError(BAD_RESPONSE)  # an answer about some other session is never used
    return session


def _extract_sessions(body: dict) -> tuple:
    """(sessions, pagination) from the documented wrapper ``{"data": {"sessions": [...], "pagination": {...}}}``."""
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise PremblyError(BAD_RESPONSE)
    sessions = tuple(_to_session(s) for s in data["sessions"] if isinstance(s, dict))
    pagination = data.get("pagination")
    return sessions, pagination if isinstance(pagination, dict) else {}


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _retry_after(response: httpx.Response) -> float:
    try:
        wait = float(response.headers.get("retry-after", ""))
    except ValueError:
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS
    return min(max(wait, 0.0), MAX_RETRY_AFTER_SECONDS)


class PremblyClient:
    def __init__(self, *, base_url: str, secret_key: str, org_id: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 transport: Optional[httpx.BaseTransport] = None):
        self._base_url = base_url.rstrip("/")
        self._secret_key = secret_key
        self._org_id = org_id
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def from_settings(cls) -> "PremblyClient":
        return cls(base_url=settings.PREMBLY_BASE_URL, secret_key=settings.PREMBLY_SECRET_KEY, org_id=settings.PREMBLY_ORG_ID)

    def get_session(self, session_id: str, *, include_payload: bool = False) -> PremblySession:
        """The authoritative record of one widget session (design K-C1: the pull, made with the SECRET key)."""
        if not _SESSION_ID.match(session_id or ""):
            raise ValueError("invalid prembly session id")
        params = {"include_payload": "true"} if include_payload else None
        body = self._get(SESSION_PATH.format(session_id=session_id), params)
        return _extract_session(body, session_id)

    def list_undelivered(self, *, page: int = 1, page_size: int = 20) -> PremblySessionPage:
        """Sessions whose webhook Prembly has not delivered (for the reconcile job). Page numbers start at 1."""
        body = self._get(UNDELIVERED_PATH, {"page": page, "page_size": page_size})
        sessions, pagination = _extract_sessions(body)
        return PremblySessionPage(sessions=sessions, page=_int(pagination.get("page")) or page,
                                  page_size=_int(pagination.get("page_size")) or page_size,
                                  total=_int(pagination.get("total")), total_pages=_int(pagination.get("total_pages")))

    def _get(self, path: str, params: Optional[dict]) -> dict:
        if not self._secret_key or not self._org_id:
            raise PremblyError(NOT_CONFIGURED)
        headers = {"x-api-key": self._secret_key, "x-organisation-id": self._org_id, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport or _transport) as http:
                response = http.get(self._base_url + path, params=params, headers=headers)
        except httpx.TimeoutException:
            logger.warning("prembly: timed out")
            raise PremblyError(TIMEOUT) from None
        except httpx.HTTPError as exc:
            logger.warning("prembly: transport error (%s)", type(exc).__name__)
            raise PremblyError(NETWORK) from None
        return self._read(response)

    @staticmethod
    def _read(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else None
        code = body.get("response_code") if body else None
        code = str(code) if code is not None else None
        failed = response.status_code >= 400 or code in _CODE_KINDS or (body is not None and body.get("status") is False)
        if failed:
            kind = classify(response.status_code if response.status_code >= 400 else None, code)
            if kind == BAD_RESPONSE:
                kind = REJECTED  # a 200 whose wrapper says ``status: false`` is a refusal, not garbage
            error = PremblyError(kind, http_status=response.status_code, response_code=code,
                                 retry_after=_retry_after(response) if kind == RATE_LIMITED else None)
            logger.warning("prembly: %s", error)
            raise error
        if body is None:
            raise PremblyError(BAD_RESPONSE, http_status=response.status_code)
        return body
