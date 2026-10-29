"""Does the person the provider verified look like the person who signed up? (design 7.3 "Mismatch", K4)

Pure functions, no database and nothing provider-specific: the seam hands over the registry name and date of birth as plain
values and these compare them with the caregiver's own Step 1 answers. Neither the provider's name nor its date of birth is ever
stored (design 10.1); only the yes/no answers are.

A result of ``None`` means "could not be compared" (one side is missing or unreadable). That is NOT a match: the engine sends
it to a human (``identity_data_missing``) rather than passing it.

A mismatch is never a rejection (design 7.3): a married name, a missing middle name or a typo in Step 1 is common, so the answer
only ever routes the attempt to the admin review queue.
"""
import hashlib
import hmac
import re
import unicodedata
from datetime import date, datetime
from typing import Optional

from app.core.config import settings

_NON_WORD = re.compile(r"[^a-z0-9]+")
_DOB_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%Y/%m/%d")
MIN_SHARED_NAME_TOKENS = 2  # one shared word ("Amina") is far too weak to call two names the same person; initials do not count


def name_tokens(name: Optional[str]) -> list[str]:
    """Lower-cased, accent-free words of a name. Order and punctuation do not matter ("BELLO, Amina" == "Amina Bello")."""
    if not isinstance(name, str):
        return []
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return [token for token in _NON_WORD.split(plain) if token]


def _token_matches(a: str, b: str) -> bool:
    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))  # an initial matches its name


def names_match(profile_name: Optional[str], provider_name: Optional[str]) -> Optional[bool]:
    """True when every word of the shorter name appears in the longer one (a missing middle name is fine) and at least
    ``MIN_SHARED_NAME_TOKENS`` words are shared. None when either name has no words."""
    a, b = name_tokens(profile_name), name_tokens(provider_name)
    if not a or not b:
        return None
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < MIN_SHARED_NAME_TOKENS:
        return False
    remaining = list(long_)
    full_words = 0
    for token in short:
        hit = next((i for i, other in enumerate(remaining) if _token_matches(token, other)), None)
        if hit is None:
            return False
        full_words += len(token) > 1 and len(remaining[hit]) > 1
        remaining.pop(hit)  # each word of the longer name can be used once
    return full_words >= MIN_SHARED_NAME_TOKENS  # initials alone ("A B") never match: they fit any name with those letters


def parse_dob(value: object) -> Optional[date]:
    """A date from what a registry sends: ISO first, then the day-first formats Nigerian records use. Unreadable -> None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()[:32]
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def dobs_match(profile_dob: Optional[date], provider_dob: Optional[date]) -> Optional[bool]:
    if profile_dob is None or provider_dob is None:
        return None
    return profile_dob == provider_dob


def normalise_id_number(number: object) -> Optional[str]:
    if isinstance(number, bool) or not isinstance(number, (str, int)):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(number)).upper()
    return cleaned if 4 <= len(cleaned) <= 40 else None


def id_fingerprint(id_type: Optional[str], number: object, *, key: Optional[str] = None) -> Optional[str]:
    """``HMAC-SHA256(KYC_FINGERPRINT_KEY, type + ":" + normalised number)`` (design K-C2): a stable handle for "the same NIN/BVN on
    two accounts" that is not the number. None when there is no number or no key (the duplicate check then cannot run; production
    refuses to boot without the key)."""
    key = settings.KYC_FINGERPRINT_KEY if key is None else key
    cleaned = normalise_id_number(number)
    if not key or cleaned is None:
        return None
    message = f"{id_type or 'unknown'}:{cleaned}"
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
