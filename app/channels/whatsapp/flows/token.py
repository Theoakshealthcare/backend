"""The flow token: what ties one open booking Flow to one draft and one contact.

We choose the token when we send the Flow message, and Meta hands it back on every data-endpoint call and in
the completion message. It is ``<draft id>.<expiry>.<signature>`` where the signature is an HMAC of the first
two parts keyed from SECRET_KEY with a domain label, so a token cannot be forged, replayed against another
draft, or used after it expires. It carries no personal data.
"""

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings

TOKEN_TTL_SECONDS = 3600
_LABEL = b"oakly-whatsapp-flow-token:"


def _key() -> bytes:
    return hashlib.sha256(_LABEL + settings.SECRET_KEY.encode()).digest()


def _sign(draft_id: str, expires: int) -> str:
    mac = hmac.new(_key(), f"{draft_id}.{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:18]).decode().rstrip("=")


def _epoch(now: datetime) -> int:
    return int(now.replace(tzinfo=timezone.utc).timestamp())


def issue(draft_id: uuid.UUID, now: Optional[datetime] = None) -> str:
    now = now or datetime.utcnow()
    expires = _epoch(now) + TOKEN_TTL_SECONDS
    return f"{draft_id}.{expires}.{_sign(str(draft_id), expires)}"


def verify(token: object, now: Optional[datetime] = None) -> Optional[uuid.UUID]:
    """The draft id a good, unexpired token names; None for anything else."""
    if not isinstance(token, str) or len(token) > 200:
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    draft_id, expires, signature = parts[0], int(parts[1]), parts[2]
    if not hmac.compare_digest(signature, _sign(draft_id, expires)):
        return None
    if _epoch(now or datetime.utcnow()) > expires:
        return None
    try:
        return uuid.UUID(draft_id)
    except ValueError:
        return None
