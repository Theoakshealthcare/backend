"""THE SEAM between Oakly's KYC code and what Dojah really sends. Every assumption the public docs do not confirm lives here.

Provider is Dojah as of 2026-10-30 (Prembly was dropped; docs/plans/2026-10-30-caregiver-kyc-dojah-delta.md). Sources are tagged
[DOCS] (Dojah's public pages), [PORT] (working code in the owner's Timpbills and Weysure) and [UNVERIFIED] (nothing of Oakly has
met the real service until the K8 live pass). When the real captures arrive, change THIS module (and ``dojah.py``'s ``_extract``);
the webhook endpoint, the ledger and the worker job read Dojah only through it.

The model: the app launches Dojah's hosted widget with a reference the SERVER minted (``OKL-<uuid4 hex>``, stored on the attempt in
``provider_session_id``). Dojah signs a webhook that names that ``reference_id``; the webhook is only a trigger. The worker PULLS
``GET /api/v1/kyc/verification?reference_id=...`` with the secret key and trusts the pull, never the webhook body (K-C1).

LIVE SANDBOX PASS (2026-10-30, three runs on a phone; SOP section "Live sandbox pass"). CONFIRMED live (sandbox keys): D1 (hex
``x-dojah-signature`` over the raw body, keyed by the webhook secret, plus an ignored ``-v2`` header), D4 (flat body: ``reference_id`` and
``verification_status`` at the top level, no ``entity`` wrapper), D5 (the widget took our ``reference_id``; the pull's ``entity`` echoes it),
D6 (``Completed`` seen), D9 (BVN entity with ``first_name`` / ``middle_name`` / ``last_name`` / ``date_of_birth`` as ``DD-Mon-YYYY``; the
sandbox returns a canned person unrelated to the BVN typed), D14's sandbox host (``https://sandbox.dojah.io``; ``api.dojah.io`` answers 401
to test keys). STILL OPEN: production score fields (D7), webhook retry schedule and the QR / other-device case (D2/D3; run 2 produced NO
webhook at all and only the reconcile cron settled it), ``Abandoned`` and relaunch (D6), the BVN entity's ``watch_listed`` string (empty in
the sandbox, values unknown) and ``aml.status`` (D11), failure ``message`` texts (D12), price per session, image retention.

UNCONFIRMED assumptions (each is also listed in the KD1 SOP for the K8 pass):

  D1  Signature. ``x-dojah-signature`` is the HEX HMAC-SHA256 of the RAW body keyed by the webhook SECRET (Developers -> Webhooks,
      "Secret" column), not by the API secret key [DOCS][PORT]. ``x-dojah-signature-v2`` (a hash of the secret alone) does not bind
      the payload and is ignored. Whether the dashboard's Secret really is the HMAC key: [UNVERIFIED].
  D2  No idempotency header is documented, so the ledger key is derived from the delivery itself: ``reference_id`` + the status word
      + the body hash. A retry of the same body is deduped; a later event with a different status or body is processed.
  D3  Retry behaviour is unknown [UNVERIFIED]: the docs only say "respond 200". A failed enqueue still answers 500 and forgets the
      ledger row (``ENQUEUE_FAILURE_STATUS``). Whether a session finished on another device (QR continue) fires the webhook, and
      whether the dashboard has a delivery log: [UNVERIFIED]. The reconcile cron covers all of it.
  D4  Event shape. Top-level ``reference_id``, ``verification_status``, ``status``, ``verification_type``, ``data``, ``metadata`` and
      no ``entity`` wrapper [DOCS]; ``referenceId`` and ``data.reference_id`` are also looked at [PORT]. Only the reference and the
      status word are read; the rest of the body is dropped, image links (``selfie_url``, ``id_url``, ``back_url``,
      ``verification_pdf``) included.
  D5  Reference echo. The widget is launched with our reference and Dojah returns it as ``reference_id`` in the webhook AND in the
      pull's ``entity`` [DOCS][PORT]. The pull must name the attempt's reference or it is not used (see ``dojah._extract``).
  D6  Status words. ``verification_status`` is ``Ongoing`` | ``Pending`` | ``Completed`` | ``Failed`` | ``Abandoned`` [DOCS][PORT].
      ``Completed`` is NOT a pass: it means the flow finished. A pass needs ``government_data.status`` AND ``selfie.status`` true and
      the face confidence over the threshold; an explicit false is a fail, but an ABSENT or unreadable result is neither
      (``Evidence.incomplete``: a person looks, so a drifting shape never costs a try nor verifies anyone). ``Failed`` is a final fail. ``Abandoned`` and ``Pending`` / ``Ongoing`` are not final:
      nothing is recorded, and the attempt expires at its TTL if it is never finished (delta: abandoned -> expired path). Whether an
      abandoned reference may be launched again: [UNVERIFIED].
  D7  Face score. ``data.selfie.confidence_value`` (0-100) against ``DOJAH_FACE_MATCH_THRESHOLD`` (default 70, as Timp and Weysure);
      the field NAME is [UNVERIFIED] in Weysure's own SOP. Dojah's pages quote 60/80/90 for different products. A value outside
      0-100 is dropped, never clamped. Without a number the face step follows ``selfie.status``. The engine's own bands
      (``KYC_FACE_FLOOR`` / ``KYC_FACE_THRESHOLD``) then apply to the same number.
      SANDBOX FINDING (live run, 2026-10-30): the pull has NO ``confidence_value``; ``selfie.data.match_score`` and
      ``selfie.data.liveness_score`` are null and only ``selfie.status`` (true) and ``message`` strings are present. OWNER DECISION
      (2026-10-30 13:35 WAT): when Dojah gives no face number AT ALL (``Evidence.face_flag_only``: the field is absent or null),
      ``selfie.status == True`` with the government check true stands in for the face check, so name/date of birth are what decide;
      a number that is present but unusable is still "missing" (review), a numeric score keeps every threshold and band, and an
      absent or false flag is missing / a fail as before. Reason: Dojah already runs liveness + face match and returns its own
      success flag, as Timp and Weysure rely on. Revisit if production returns scores (and read ``selfie.data.match_score``).
  D8  Liveness. No separate score is known: liveness is ``selfie.status`` (a boolean, probably liveness AND match in one flag
      [UNVERIFIED]; the sandbox's ``selfie.data.liveness_score`` is null). ``Evidence.liveness_passed`` carries it; ``identity_verifications.liveness_confidence`` stays NULL.
  D9  Registry identity (K4). ``data.government_data.data.<bvn|nin>.entity`` with ``first_name``, ``middle_name``, ``last_name``,
      ``date_of_birth`` (``01-Jun-1982`` seen; ``kyc_matching.parse_dob`` takes seven formats) and the ID number under the type's
      own key [PORT, confirmed live on Timp 2026-07-11]. Whether a NIN answer carries ``date_of_birth`` and the number: [UNVERIFIED].
      Missing = not compared: the engine sends the attempt to a human (``identity_data_missing``).
  D10 ID type. ``verification_type`` (``BVN`` / ``NIN``), else the one key present under ``government_data.data``. Driver's licence,
      voter's card and passport depend on how the owner configures the flow: the widget is built for BVN and NIN, others are left NULL.
  D11 Watch list. No equivalent known (``aml.status`` exists in the webhook, meaning [UNVERIFIED]): ``Evidence.watchlisted`` is
      always False for now.
  D12 Failure reason. ``message`` fields are free text; ``_failure_code`` maps a few keywords onto our reason codes and anything
      else is ``provider_failed``.
  D13 Body cap. 1 MB is far above any documented payload.
  D14 Cost. The pull is not known to carry a price: ``provider_cost`` stays NULL. Sandbox vs production base URL and app ids: [UNVERIFIED].

Nothing here stores a body or a person's details: the functions return small typed values and drop the rest. The registry name,
date of birth and number live in an ``Evidence`` (in memory, hidden from ``repr``) for the length of one decision.
"""
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.core.config import settings
from app.services.dojah import ID_TYPES, REFERENCE_ID_PATTERN, DojahVerification
from app.services.kyc_decision import Evidence
from app.services.kyc_matching import parse_dob

SIGNATURE_HEADER = "x-dojah-signature"  # D1
MAX_BODY_BYTES = 1_000_000  # D13
ENQUEUE_FAILURE_STATUS = 500  # D3: our "please retry" answer; whether Dojah retries is unknown
REFERENCE_PREFIX = "OKL-"
_OUR_REFERENCE = re.compile(r"OKL-[0-9a-f]{32}")
_ANY_REFERENCE = re.compile(REFERENCE_ID_PATTERN)

PASS, FAIL = "pass", "fail"
# D6 (``Completed`` is handled apart: it is a finished flow, not a verdict)
FAILED_WORDS, OPEN_WORDS = frozenset({"failed"}), frozenset({"pending", "ongoing"})
ABANDONED = "abandoned"
SCORE_SCALE = 100


def mint_reference() -> str:
    """D5: the provider reference of a new attempt, minted BEFORE the widget is launched. Unguessable (122 random bits)."""
    return REFERENCE_PREFIX + uuid.uuid4().hex


def is_our_reference(value: Any) -> bool:
    """Is ``value`` shaped like a reference this server minted? A signed event for anything else is not ours (the same Dojah
    account may serve other widgets) and is acknowledged without being recorded."""
    return isinstance(value, str) and _OUR_REFERENCE.fullmatch(value) is not None


def webhook_secret() -> str:
    """D1: the key the webhook signature is checked with. Empty means "not configured" and nothing verifies."""
    return settings.DOJAH_WEBHOOK_SECRET


def verify_signature(raw_body: bytes, header: Optional[str], secret: str) -> bool:
    """D1. Hex HMAC-SHA256 over the RAW bytes, compared in constant time and as bytes (a non-ASCII header is a plain mismatch, not
    an exception). An empty secret never verifies (HMAC with an empty key is something anyone can compute, so "verifying" with it
    would be a forgery hole disguised as a check); neither does a missing or empty header."""
    if not secret or not header or not header.strip():
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.strip().lower().encode("utf-8", "replace"))


def dedupe_key(raw_body: bytes, event: "WebhookEvent") -> str:
    """D2: the ledger's unique key for one delivery: reference + status word + body hash, itself hashed (fixed length, nothing
    readable). The same body twice is one event; the same reference with another status or body is another."""
    material = "\n".join((event.reference_id, event.verification_status or "", hashlib.sha256(raw_body).hexdigest()))
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class WebhookEvent:
    """The few facts read from a webhook body. The body itself is never kept (design 8.2)."""
    reference_id: str  # D4: shaped like a URL-safe token, but not necessarily one of ours (``is_our_reference``)
    verification_status: Optional[str]  # D6: one of Dojah's five words, lower-cased; None when absent or unknown
    claim: Optional[str]  # FAIL when the webhook says Failed, else None: the webhook cannot say "pass" (Completed is not a pass)
    settled: bool  # the webhook says the flow reached an end (Completed or Failed): the pull should show a verdict


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _word(value: Any) -> Optional[str]:
    return value.strip().lower().replace(" ", "_") if isinstance(value, str) else None


def parse_event(payload: Any) -> Optional[WebhookEvent]:
    """D4/D6. None when the body is not a JSON object with a usable ``reference_id`` (the endpoint answers 400)."""
    body = _dict(payload)
    reference = next((c for c in (body.get("reference_id"), body.get("referenceId"), _dict(body.get("data")).get("reference_id"))
                      if isinstance(c, str) and c), None)
    if reference is None or not _ANY_REFERENCE.fullmatch(reference):
        return None
    word = _word(body.get("verification_status"))
    known = FAILED_WORDS | OPEN_WORDS | {"completed", ABANDONED}
    word = word if word in known else None
    return WebhookEvent(reference_id=reference, verification_status=word, claim=FAIL if word in FAILED_WORDS else None,
                        settled=word == "completed" or word in FAILED_WORDS)


@dataclass(frozen=True)
class Observation:
    """What the PULLED verification says (the trusted confirmation, design 8.4), reduced to what is recorded and decided on. The
    scalar fields hold no name or number; those are only in ``evidence`` (K4), which is in memory and hidden from ``repr``."""
    final: bool  # Dojah reached a verdict (D6)
    verdict: Optional[str]  # PASS / FAIL when final
    face_confidence: Optional[Decimal]
    abandoned: bool = False  # D6: the person left the widget; not final
    evidence: Optional[Evidence] = field(default=None, repr=False)  # set when final: what the K4 decision rules read


def _score(value: Any) -> Optional[Decimal]:
    """D7: a score on 0..100, or None when absent or outside the scale."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number > SCORE_SCALE:
        return None
    return number.quantize(Decimal("0.01"))


def observe(verification: DojahVerification) -> Observation:
    status = _word(verification.verification_status)
    face = _score(verification.selfie_confidence)
    if status == ABANDONED:
        return Observation(final=False, verdict=None, face_confidence=None, abandoned=True)
    if status not in FAILED_WORDS and status != "completed":
        return Observation(final=False, verdict=None, face_confidence=None)  # Pending, Ongoing, or a word we do not know
    verdict = FAIL if status in FAILED_WORDS else _completed_verdict(verification, face)
    incomplete = verdict == PASS and (verification.government_status is None or verification.selfie_status is None)
    registry = verification.registry
    id_type = verification.registry_type if verification.registry_type in ID_TYPES else None
    evidence = Evidence(
        verdict=verdict, face_confidence=face, liveness_passed=verification.selfie_status, watchlisted=False,  # D8, D11
        failure_reason=_failure_code(verification.message), id_type=id_type,
        id_number=_text(registry.get(id_type)) if id_type else None, registry_name=_registry_name(registry),
        registry_dob=parse_dob(_text(registry.get("date_of_birth"))), incomplete=incomplete,
        face_flag_only=not verification.selfie_confidence_given)
    return Observation(final=True, verdict=verdict, face_confidence=face, evidence=evidence)


def _completed_verdict(verification: DojahVerification, face: Optional[Decimal]) -> str:
    """D6/D7: ``Completed`` passes only when the government record AND the selfie are both true and the face number (when Dojah gives
    one) reaches the threshold. An explicit false is a fail. A result that is ABSENT or unreadable is neither: it returns PASS with
    ``Evidence.incomplete`` set (see ``observe``), which the rules turn into ``needs_review`` (``identity_data_missing``), never into a
    pass, and never into a failed attempt that would cost the caregiver a try because Dojah's shape drifted."""
    if verification.government_status is False or verification.selfie_status is False:
        return FAIL
    if face is not None and face < settings.DOJAH_FACE_MATCH_THRESHOLD:
        return FAIL
    return PASS


def _text(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()[:200]
    return None


def _registry_name(registry: dict) -> Optional[str]:
    """D9: the registry's name parts joined."""
    parts = (_text(registry.get(key)) for key in ("first_name", "middle_name", "last_name"))
    return " ".join(p for p in parts if p) or None


_FAILURE_KEYWORDS = (  # D12: (substring of the provider's free text, our reason code); first hit wins
    ("liveness", "liveness_failed"), ("not found", "record_not_found"), ("no record", "record_not_found"),
    ("unreadable", "document_unreadable"), ("blur", "document_unreadable"), ("document", "document_unreadable"),
    ("face", "face_mismatch"),
)


def _failure_code(reason: Optional[str]) -> Optional[str]:
    text = (reason or "").casefold()
    return next((code for needle, code in _FAILURE_KEYWORDS if needle in text), None)
