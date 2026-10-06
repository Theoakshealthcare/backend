import random
import string
from datetime import datetime, timedelta
from typing import Any, Optional, Union

from jose import jwt
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


def create_access_token(
    subject: Union[str, Any], expires_delta: Optional[timedelta] = None
) -> str:
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode = {"exp": expire, "sub": str(subject), "type": "access"}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_refresh_token(
    subject: Union[str, Any], expires_delta: Optional[timedelta] = None
) -> str:
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
    to_encode = {"exp": expire, "sub": str(subject), "type": "refresh"}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def verify_refresh_token(token: str) -> Optional[str]:
    try:
        decoded_token = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        if decoded_token.get("type") != "refresh":
            return None
        return decoded_token["sub"]
    except jwt.JWTError:
        return None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _password_hash.verify(_bcrypt_input(plain_password), hashed_password)


def get_password_hash(password: str) -> str:
    return _password_hash.hash(_bcrypt_input(password))


def generate_otp(length: int = 5) -> str:
    """Generate a random OTP code of specified length."""
    return ''.join(random.choices(string.digits, k=length))


def generate_password_reset_token(email: str) -> str:
    delta = timedelta(hours=settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS)
    now = datetime.utcnow()
    expires = now + delta
    exp = expires.timestamp()
    encoded_jwt = jwt.encode(
        {"exp": exp, "nbf": now, "sub": email},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    return encoded_jwt


def verify_password_reset_token(token: str) -> Optional[str]:
    try:
        decoded_token = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return decoded_token["sub"]
    except jwt.JWTError:
        return None