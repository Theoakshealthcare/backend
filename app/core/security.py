import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from cryptography.fernet import Fernet, MultiFernet

from app.core.config import settings

# bcrypt only reads the first 72 bytes of a password. passlib silently cut longer ones there, and
# every stored hash was made that way, so we cut explicitly: bcrypt>=5 would otherwise raise, and
# a long-password user who registered under passlib could no longer sign in.
BCRYPT_MAX_BYTES = 72

_password_hash = PasswordHash((BcryptHasher(rounds=12),))


def _bcrypt_input(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]

ALGORITHM = "HS256"

def _build_fernet(keys: str) -> MultiFernet:
    """Build a MultiFernet from a comma-separated key list.

    The first key encrypts; every key can decrypt. To rotate, prepend the new key,
    deploy, run scripts/reencrypt_health_data.py, then drop the old key.
    """
    parsed = [k.strip() for k in keys.split(",") if k.strip()]
    if not parsed:
        raise RuntimeError("VITALS_ENCRYPTION_KEY is empty.")
    return MultiFernet([Fernet(k) for k in parsed])


fernet = _build_fernet(settings.VITALS_ENCRYPTION_KEY)


def encrypt(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()


def rotate_ciphertext(value: str) -> str:
    """Re-encrypt a token with the primary (first) key. Raises InvalidToken if no key can read it."""
    return fernet.rotate(value.encode()).decode()


def _create_token(subject: Union[str, Any], token_type: str, expires_in: timedelta, token_version: int = 0) -> str:
    # ``tv`` is users.token_version when the token was issued; a password change bumps it, which ends every older token.
    claims = {"exp": datetime.now(timezone.utc) + expires_in, "sub": str(subject), "type": token_type,
              "tv": token_version}
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_app_token(token: str, expected_type: str) -> dict:
    """Verify an app-user token and return its claims; raises ``jwt.PyJWTError`` on any problem.

    HS256 only; ``exp``, ``sub`` and ``type`` are required. No audience is passed, so PyJWT refuses
    any token that carries ``aud`` -- that is what keeps admin tokens (aud="admin") off app routes.
    """
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub", "type"]},
    )
    if payload["type"] != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def create_access_token(
    subject: Union[str, Any], expires_delta: Optional[timedelta] = None, token_version: int = 0
) -> str:
    return _create_token(
        subject, "access", expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES), token_version
    )


def create_refresh_token(
    subject: Union[str, Any], expires_delta: Optional[timedelta] = None, token_version: int = 0
) -> str:
    return _create_token(
        subject, "refresh", expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), token_version
    )


def token_version_is_current(claims: dict, current_version: int) -> bool:
    """False for a token issued before the user's last password change. A token minted before ``tv`` existed has no
    claim and counts as version 0, so deploying this logs nobody out."""
    return claims.get("tv", 0) == (current_version or 0)


def verify_refresh_token(token: str) -> Optional[str]:
    try:
        return decode_app_token(token, "refresh")["sub"]
    except jwt.PyJWTError:
        return None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _password_hash.verify(_bcrypt_input(plain_password), hashed_password)


def get_password_hash(password: str) -> str:
    return _password_hash.hash(_bcrypt_input(password))


def generate_otp(length: int = 5) -> str:
    """Generate a random OTP code of specified length."""
    return ''.join(random.choices(string.digits, k=length))


def generate_password_reset_token(email: str) -> str:
    return _create_token(email, "password_reset", timedelta(hours=settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS))


def verify_password_reset_token(token: str) -> Optional[str]:
    try:
        return decode_app_token(token, "password_reset")["sub"]
    except jwt.PyJWTError:
        return None
