"""THE SEAM between Oakly's KYC code and what Prembly really sends. Every assumption the public docs do not confirm lives here.

Built against the PUBLIC docs only (docs/plans/2026-10-27-caregiver-kyc-design.md sections 3 and 15). Until the K0 spike and the K8
live pass nothing below has met the real service. When the real captures arrive, change THIS module (and ``prembly.py``'s
``SESSION_PATH`` / ``_extract_session``); the webhook endpoint, the ledger and the worker job read Prembly only through it.

UNCONFIRMED assumptions (each is also listed in the K3 SOP):

  U1  Signing key. ``x-prembly-signature`` is the base64 HMAC-SHA256 of the raw body keyed by ``PREMBLY_WEBHOOK_KEY``. Prembly's page
      calls the key the *public* key (the one inside the app). If so a signature proves little, which is why a webhook never sets
      anything by itself: the worker PULLS the session with the secret key and trusts the pull (K-C1, ``kyc_events``). (Q4)
  U2  Signature encoding. Standard base64 of the raw digest, compared as text. Hex or URL-safe base64 would be refused. (Q4/Q5)
  U3  Idempotency header. ``token`` is unique per webhook and a RETRY of the same webhook carries the same ``token``. (Q5)
  U4  Retry behaviour. Prembly "may retry on 500" (no schedule or count published), so a failed enqueue answers 500 and the ledger
      row is removed to let the retry through (``ENQUEUE_FAILURE_STATUS``). No timestamp or replay window exists. (Q5)
  U5  Event shape. ``event`` and ``session_id`` at the top level (the SDK sample); ``session_id`` is also looked for under ``data``.
      Event NAMES are not trusted for anything: only ``verification.completed`` is documented, so every event with a session id is
      handled the same way (the pull decides), and unknown names are logged, not rejected. (Q5)
  U6  Metadata echo. Whether our ``oakly_ref`` comes back in the webhook, and where (``metadata``, ``data.metadata`` or
      ``data.widget_info.user_ref``), and whether it comes back in the session pull (``payload.metadata`` etc.). When it is not
      echoed, the attempt is found by the client's session-id hint and bound by the end user's e-mail instead. (Q2)
  U7  Webhook claim. ``verification.status`` (``VERIFIED`` in the sample) is the webhook's verdict, compared with the pull's. (Q1/Q6)
  U8  Pull verdict. The session's ``overall_result`` says pass or fail ("passed" in the docs; the other values are guesses, see
      ``PASS_WORDS`` / ``FAIL_WORDS``). A value in neither list means "not final": nothing is recorded as an outcome. (Q3)
  U9  Scores. Read from the pull's ``payload`` (``include_payload=true``): ``face_data.confidence`` and
      ``biometric_results.liveness_confidence``. The scale is unknown (Q6): the face score is taken as 0-100 and the liveness score
      as 0-1 (``FACE_SCORE_SCALE`` / ``LIVENESS_SCORE_SCALE``); a value outside its scale is dropped, never clamped.
  U10 Provider reference. ``payload.verification.reference`` is Prembly's reference for the check.
  U11 Body cap. 1 MB is far above any documented payload (Q5 asks Prembly for the real maximum).
  U12 Registry identity (K4). The registry's name, date of birth and ID number come back in the pull's ``payload.verification_response``
      (the NIN / BVN samples): a full name or first / middle / last parts, a date under ``birthdate`` / ``dateOfBirth`` / ``dob``,
      the number under ``number``. Not found there = not compared: the engine sends the attempt to a human (``identity_data_missing``).
      The session's own top-level ``full_name`` is NOT used: it is what the widget was pre-filled with, not the registry's. (Q1)
  U13 ID type. ``payload.id_type`` (or ``verification_response.id_type``) is one of ``nin``, ``bvn``, ``drivers_licence``,
      ``voters_card``, ``passport``; anything else is left NULL. (Q1)
  U14 Watch list. ``verification_response.watchListed`` (BVN sample, code ``07``) is true for a watch-listed person. (Q1)
  U15 Failure reason. ``failure_reason`` is free text; ``_failure_code`` maps a few keywords (liveness, not found, unreadable,
      face) onto our reason codes and anything else is ``provider_failed``. (Q6)

Nothing here stores a body or a person's details: the functions return small typed values and drop the rest. The registry name,
date of birth and number live in an ``Evidence`` (in memory, hidden from ``repr``) for the length of one decision.
"""
import base64
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.core.config import settings
from app.db.models.identity_verification import ID_TYPES
from app.services.kyc_decision import Evidence
from app.services.kyc_matching import parse_dob
from app.services.prembly import SESSION_ID_PATTERN, PremblySession

SIGNATURE_HEADER = "x-prembly-signature"
TOKEN_HEADER = "token"
MAX_BODY_BYTES = 1_000_000  # U11
ENQUEUE_FAILURE_STATUS = 500  # U4: the status Prembly documents as "may retry"
MAX_TOKEN_LENGTH = 200
MAX_EVENT_TYPE_LENGTH = 64

PASS_WORDS = frozenset({"verified", "passed", "pass", "success", "successful", "approved"})  # U7/U8
FAIL_WORDS = frozenset({"failed", "fail", "declined", "rejected", "unverified", "not_verified"})
FACE_SCORE_SCALE = 100  # U9
LIVENESS_SCORE_SCALE = 1

PASS, FAIL = "pass", "fail"
_SESSION_ID = re.compile(SESSION_ID_PATTERN)
_EVENT_TYPE = re.compile(r"[A-Za-z0-9_.:-]+")


def webhook_signing_key() -> str:
    """U1: the key the webhook signature is checked with. Empty means "not configured" and nothing verifies."""
    return settings.PREMBLY_WEBHOOK_KEY


def verify_signature(raw_body: bytes, header: Optional[str], key: str) -> bool:
    """U1/U2. Over the RAW bytes, compared in constant time. An empty key never verifies (HMAC with an empty key is something anyone
    can compute, so "verifying" with it would be a forgery hole disguised as a check); neither does a missing or empty header."""
    if not key or not header or not header.strip():
        return False
    expected = base64.b64encode(hmac.new(key.encode(), raw_body, hashlib.sha256).digest())
    return hmac.compare_digest(expected, header.strip().encode("utf-8", "replace"))


def valid_token(token: Optional[str]) -> bool:
    """U3: a printable, bounded string (it is stored and used as a unique key)."""
    return bool(token) and len(token) <= MAX_TOKEN_LENGTH and token.isprintable() and token == token.strip()


@dataclass(frozen=True)
class WebhookEvent:
    """The few facts read from a webhook body. The body itself is never kept (design 8.2)."""
    session_id: str
    event_type: Optional[str]  # sanitised; informational only (U5)
    oakly_ref: Optional[uuid.UUID]  # U6
    claim: Optional[str]  # PASS / FAIL / None (U7)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _word(value: Any) -> Optional[str]:
    return value.strip().lower().replace(" ", "_") if isinstance(value, str) else None


def _verdict(value: Any) -> Optional[str]:
    word = _word(value)
    return PASS if word in PASS_WORDS else FAIL if word in FAIL_WORDS else None


def _as_ref(value: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _echoed_ref(container: dict) -> Optional[uuid.UUID]:
    """U6: our ``oakly_ref`` wherever Prembly might echo it inside ``container`` (a webhook body or a pull's payload)."""
    data = _dict(container.get("data"))
    for candidate in (_dict(container.get("metadata")).get("oakly_ref"), _dict(data.get("metadata")).get("oakly_ref"),
                      _dict(data.get("widget_info")).get("user_ref")):
        ref = _as_ref(candidate)
        if ref is not None:
            return ref
    return None


def parse_event(payload: Any) -> Optional[WebhookEvent]:
    """U5-U7. None when the body is not a JSON object with a usable ``session_id`` (the endpoint answers 400)."""
    body = _dict(payload)
    session_id = body.get("session_id") or _dict(body.get("data")).get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    event_type = body.get("event")
    if not (isinstance(event_type, str) and event_type and len(event_type) <= MAX_EVENT_TYPE_LENGTH
            and _EVENT_TYPE.fullmatch(event_type)):
        event_type = None
    return WebhookEvent(session_id=session_id, event_type=event_type, oakly_ref=_echoed_ref(body),
                        claim=_verdict(_dict(body.get("verification")).get("status")))


@dataclass(frozen=True)
class Observation:
    """What the PULLED session says (the trusted confirmation, design 8.4), reduced to what is recorded and decided on. The
    scalar fields hold no name, e-mail or number; those are only in ``evidence`` (K4), which is in memory and hidden from ``repr``."""
    final: bool  # the provider reached a verdict (U8)
    verdict: Optional[str]  # PASS / FAIL when final
    face_confidence: Optional[Decimal]
    liveness_confidence: Optional[Decimal]
    provider_reference: Optional[str]
    echoed_ref: Optional[uuid.UUID]
    evidence: Optional[Evidence] = field(default=None, repr=False)  # set when final: what the K4 decision rules read


def _score(value: Any, scale: int) -> Optional[Decimal]:
    """U9: a score on 0..``scale`` normalised to 0..100, or None when absent or outside its scale."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number > scale:
        return None
    return (number * 100 / scale).quantize(Decimal("0.01"))


def observe(session: PremblySession) -> Observation:
    verdict = _verdict(session.overall_result)
    payload = session.payload or {}
    face = _score(_dict(payload.get("face_data")).get("confidence"), FACE_SCORE_SCALE)
    liveness = _score(_dict(payload.get("biometric_results")).get("liveness_confidence"), LIVENESS_SCORE_SCALE)
    registry = _dict(payload.get("verification_response"))
    evidence = None
    if verdict is not None:
        evidence = Evidence(
            verdict=verdict, face_confidence=face, liveness_confidence=liveness, watchlisted=_watchlisted(registry),
            failure_reason=_failure_code(session.failure_reason), id_type=_id_type(payload, registry),
            id_number=_text(registry.get("number")), registry_name=_registry_name(registry),
            registry_dob=_registry_dob(registry))
    return Observation(
        final=verdict is not None, verdict=verdict, face_confidence=face, liveness_confidence=liveness,
        provider_reference=_reference(_dict(payload.get("verification")).get("reference")),  # U10
        echoed_ref=_echoed_ref(payload), evidence=evidence,
    )


def _text(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()[:200]
    return None


def _first(container: dict, *keys: str) -> Optional[str]:
    return next((t for t in (_text(container.get(k)) for k in keys) if t), None)


def _registry_name(registry: dict) -> Optional[str]:
    """U12: the registry's full name, or its parts joined."""
    full = _first(registry, "full_name", "fullName", "name")
    if full:
        return full
    parts = [_first(registry, *keys) for keys in (("firstname", "first_name", "firstName"), ("middlename", "middle_name", "middleName"),
                                                 ("lastname", "last_name", "lastName", "surname"))]
    return " ".join(p for p in parts if p) or None


def _registry_dob(registry: dict):
    return parse_dob(_first(registry, "birthdate", "dateOfBirth", "date_of_birth", "dob"))


def _id_type(payload: dict, registry: dict) -> Optional[str]:
    word = _word(payload.get("id_type")) or _word(registry.get("id_type"))
    return word if word in ID_TYPES else None


def _watchlisted(registry: dict) -> bool:
    return any(registry.get(key) is True for key in ("watchListed", "watchlisted", "watch_listed"))


_FAILURE_KEYWORDS = (  # U15: (substring of the provider's free text, our reason code); first hit wins
    ("liveness", "liveness_failed"), ("not found", "record_not_found"), ("no record", "record_not_found"),
    ("unreadable", "document_unreadable"), ("blur", "document_unreadable"), ("document", "document_unreadable"),
    ("face", "face_mismatch"),
)


def _failure_code(reason: Optional[str]) -> Optional[str]:
    text = (reason or "").casefold()
    return next((code for needle, code in _FAILURE_KEYWORDS if needle in text), None)


def _reference(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def same_email(a: Optional[str], b: Optional[str]) -> bool:
    """U6 fallback binding: the e-mail Prembly recorded for the session is the account's own."""
    return bool(a) and bool(b) and a.strip().casefold() == b.strip().casefold()
