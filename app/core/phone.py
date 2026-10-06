"""Phone numbers: E.164 normalisation and log-safe references.

Oakly is Nigeria-first, so a national number (``0801 234 5678``) is read as Nigerian. A WhatsApp
``wa_id`` is the number in international digits with no ``+`` (``2348012345678``).
"""

import hashlib
import re
from typing import Any, Optional, Set

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_NG_PREFIX = "+234"
_NG_SUBSCRIBER_DIGITS = 10  # after the country code, with no trunk zero


def normalize_e164(raw: Any, default_region: str = "NG") -> Optional[str]:
    """Return ``+<country><number>`` or None when ``raw`` is not a plausible phone number."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or re.search(r"[A-Za-z]", text):
        return None
    cleaned = re.sub(r"[\s\-().]", "", text)
    if not re.fullmatch(r"\+?\d+", cleaned):
        return None

    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif cleaned.startswith("00"):
        digits = cleaned[2:]
    elif cleaned.startswith("0") and default_region == "NG":
        digits = "234" + cleaned[1:]
    elif default_region == "NG" and len(cleaned) == _NG_SUBSCRIBER_DIGITS and cleaned[0] in "789":
        digits = "234" + cleaned  # national number written without the trunk zero
    else:
        digits = cleaned  # already international (a wa_id)

    candidate = "+" + digits
    if not _E164.match(candidate):
        return None
    if candidate.startswith(_NG_PREFIX) and len(candidate) - len(_NG_PREFIX) != _NG_SUBSCRIBER_DIGITS:
        return None
    return candidate


def phone_variants(e164: str) -> Set[str]:
    """Every spelling an older account might have stored for this number."""
    variants = {e164, e164[1:]}
    if e164.startswith(_NG_PREFIX):
        variants.add("0" + e164[len(_NG_PREFIX):])
    return variants


def mask_phone(value: Optional[str]) -> str:
    """For logs: keep the country code and the last four digits."""
    if not value:
        return "-"
    plus = "+" if value.startswith("+") else ""
    digits = value.lstrip("+")
    if len(digits) <= 7:
        return plus + "****"
    return f"{plus}{digits[:3]}****{digits[-4:]}"


def contact_ref(wa_id: str) -> str:
    """A stable, non-reversible handle for a contact, for logs and metrics (never the number)."""
    return hashlib.sha256(wa_id.encode()).hexdigest()[:12]
