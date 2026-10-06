"""Tokens and TOTP for admin accounts.

Admin JWTs are deliberately incompatible with app-user JWTs:
- they carry ``aud="admin"``, which the app-user decoder (PyJWT, no audience) refuses;
- they are signed with a key derived from SECRET_KEY for this purpose, so an app token would fail
  the signature check here even if its claims were forged to look right.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
import pyotp

from app.core.config import settings

ADMIN_AUDIENCE = "admin"
TOTP_INTERVAL = 30
TOTP_WINDOW = 1  # accept one step either side, for clock drift


class AdminTokenError(Exception):
    """The token is missing, malformed, expired, of the wrong type, or not an admin token."""


def _signing_key() -> str:
    return hashlib.sha256(f"oakly-admin-jwt:{settings.SECRET_KEY}".encode()).hexdigest()


def create_admin_token(subject: Any, token_type: str, expires_in: timedelta, **claims: Any) -> str:
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": str(subject),
        "aud": ADMIN_AUDIENCE,
        "type": token_type,
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def decode_admin_token(token: str, expected_type: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            _signing_key(),
            algorithms=["HS256"],
            audience=ADMIN_AUDIENCE,
            options={"require": ["exp", "sub", "aud", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise AdminTokenError(str(exc)) from exc
    if payload.get("type") != expected_type:
        raise AdminTokenError("wrong token type")
    return payload


def hash_token(raw: str) -> str:
    """SHA-256 of an opaque token (refresh and invite tokens are stored only as hashes)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


# --- TOTP ------------------------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret, interval=TOTP_INTERVAL).provisioning_uri(
        name=email, issuer_name=settings.ADMIN_TOTP_ISSUER
    )


def matching_totp_step(secret: str, code: str, now_ts: float) -> Optional[int]:
    """The time step whose code equals ``code`` (within the drift window), else None.

    Returned as a step number so the caller can refuse a step it has already accepted.
    """
    code = (code or "").strip()
    if not (code.isascii() and code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL)
    current = int(now_ts // TOTP_INTERVAL)
    match: Optional[int] = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        # compare every candidate (no early exit) in constant time
        if hmac.compare_digest(totp.at(step * TOTP_INTERVAL), code):
            match = step
    return match
