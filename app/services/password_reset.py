"""Password reset by a short emailed code.

``request_code`` (POST /auth/password-recovery) stores a 5-digit code for the user, hashed, and returns it so the caller
can email it. ``reset_with_code`` (POST /auth/reset-password {email, code, new_password}) checks it and sets the password.
``reset_with_legacy_token`` serves the deprecated ``{token, new_password}`` form for the shipped caregiver app.

Properties that matter, and where they come from:

* The code is only ever stored as ``HMAC-SHA256(key derived from SECRET_KEY, "<user id>:<code>")``. Five digits are
  trivially brute-forced from a bare hash; with a keyed hash a database leak alone does not reveal them. Compared with
  ``hmac.compare_digest``.
* Wrong codes are counted **per exact email address, whether or not an account exists** (``admin_rate_limits`` key
  ``prc:<sha256(email)>``), so the answer to a guess is the same for every address and cannot be used to find out who is
  registered. Requests are throttled the same way (``prr:<sha256(email)>``). A second, cumulative budget
  (``prd:<sha256(email)>``, 20 wrong codes per 24 h across **all** codes) is not cleared by asking for a new code, so
  fresh codes cannot be used to keep guessing; once spent, resets are 429 with Retry-After and no code is mailed until
  the window ends. The email is hashed so the table holds no addresses. ``prr``/``prc`` rows are purged after two idle
  hours, ``prd`` rows after the day is over.
* Everything for one address runs under a Postgres advisory lock on that address, so a burst of concurrent guesses is
  checked and counted one at a time (none slips past the limit) and a code works once. The user row is then locked for
  the write. Recovery takes the locks in the same order, so the two cannot deadlock.
* A weak new password is refused **first**, before the code is looked at, counted or consumed: it depends on nothing
  but the password, so it reveals nothing and the user can retry with the same code.
* A reset bumps ``users.token_version`` (every other session ends, as in change-password) and clears the code, the
  legacy token and the counters. It issues no tokens: the app signs in with the new password.
* Nothing here logs a code, a token or a password; log lines carry the user id (or, for an unknown address, nothing
  identifying) only.
"""
import hashlib
import hmac
import logging
import re
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, case, delete, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_password_reset_token, get_password_hash, verify_password_reset_token
from app.db.models.admin import AdminRateLimit
from app.db.models.user import User
from app.services.change_password import WeakPassword, validate_new_password

logger = logging.getLogger(__name__)

CODE_LENGTH = 5  # same as the email verification code (UserService.generate_verification_code)
_CODE_RE = re.compile(r"[0-9]{%d}" % CODE_LENGTH)
_EPOCH = datetime(1970, 1, 1)  # window_start of a counter row (the other limiters' convention)
_WINDOW = timedelta(hours=1)  # counters idle for this long start again; a code lives far less
_PURGE_AFTER = timedelta(hours=2)
_DAY = timedelta(hours=24)
_REQUEST_PREFIX, _ATTEMPT_PREFIX, _DAILY_PREFIX = "prr:", "prc:", "prd:"

__all__ = [
    "PasswordResetError", "InvalidOrExpiredCode", "TooManyCodeAttempts", "TooManyCodeAttemptsToday", "LegacyTokenInvalid", "WeakPassword",
    "PasswordResetService", "CODE_LENGTH",
]


def _now() -> datetime:
    """Naive UTC, like every other timestamp in this schema. A function so tests can pin the clock."""
    return datetime.utcnow()


class PasswordResetError(Exception):
    status_code = 400
    code = ""
    message = ""


class InvalidOrExpiredCode(PasswordResetError):
    """Wrong, expired, already used, never issued, or the address has no account: deliberately one answer."""
    code = "invalid_or_expired_code"
    message = "This code is incorrect or has expired. Request a new code."


class TooManyCodeAttempts(PasswordResetError):
    status_code = 429
    code = "too_many_attempts"
    message = "Too many incorrect attempts. Request a new code."
    retry_after: Optional[int] = None  # only the 24-hour budget has a wait to announce


class TooManyCodeAttemptsToday(TooManyCodeAttempts):
    """The 24-hour budget across all codes of the address is spent: a new code does not help until the window ends."""

    def __init__(self, retry_after: int):
        super().__init__(retry_after)
        self.retry_after = retry_after
        self.message = "Too many incorrect attempts. Please try again later."


class LegacyTokenInvalid(PasswordResetError):
    """The deprecated token form keeps the plain-string ``detail`` the shipped app already handles."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.message = status_code, detail


def _email_hash(email: str) -> str:
    # Exact, like the account lookup (``User.email == email``): a case variant that matches no account must not share
    # counters with the real one, or it could reset the real address's guess count without mailing anything.
    return hashlib.sha256(email.strip().encode("utf-8")).hexdigest()[:32]


def _code_key() -> bytes:
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), b"oakly:password-reset-code:v1", hashlib.sha256).digest()


def hash_code(user_id: UUID, code: str) -> str:
    return hmac.new(_code_key(), f"{user_id}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(CODE_LENGTH))


class PasswordResetService:
    def __init__(self, db: Session):
        self.db = db

    # --- helpers -----------------------------------------------------------------------------------------
    def _lock_address(self, ehash: str) -> None:
        """Serialise everything for this address until the transaction ends (works for addresses with no account)."""
        self.db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"pwreset:{ehash}"})

    def _locked_user(self, email: str) -> Optional[User]:
        return self.db.execute(
            select(User).where(User.email == email).with_for_update().execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _purge_stale(self, now: datetime) -> None:
        # SKIP LOCKED: two requests purging at once must not wait for (or deadlock on) each other's rows.
        stale = select(AdminRateLimit.key, AdminRateLimit.window_start).where(or_(
            and_(or_(AdminRateLimit.key.like(f"{_REQUEST_PREFIX}%"), AdminRateLimit.key.like(f"{_ATTEMPT_PREFIX}%")),
                 AdminRateLimit.updated_at < now - _PURGE_AFTER),
            and_(AdminRateLimit.key.like(f"{_DAILY_PREFIX}%"), AdminRateLimit.updated_at < now - _DAY - _PURGE_AFTER),
        )).with_for_update(skip_locked=True).subquery()
        self.db.execute(delete(AdminRateLimit).where(
            tuple_(AdminRateLimit.key, AdminRateLimit.window_start).in_(select(stale.c.key, stale.c.window_start))))

    def _wrong_attempts(self, key: str, now: datetime) -> int:
        row = self.db.execute(
            select(AdminRateLimit.count, AdminRateLimit.updated_at)
            .where(AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH)).first()
        if row is None or row.updated_at < now - _WINDOW:
            return 0
        return row.count

    def _count_wrong_attempt(self, key: str, now: datetime) -> None:
        stale = AdminRateLimit.updated_at < now - _WINDOW
        self.db.execute(
            pg_insert(AdminRateLimit).values(key=key, window_start=_EPOCH, count=1, updated_at=now)
            .on_conflict_do_update(
                index_elements=[AdminRateLimit.key, AdminRateLimit.window_start],
                set_={"count": case((stale, 1), else_=AdminRateLimit.count + 1), "updated_at": now}))

    def _daily_wait(self, key: str, now: datetime) -> int:
        """Seconds left of the 24-hour lockout, 0 if the address is not locked out. ``blocked_until`` is the window's end."""
        row = self.db.execute(
            select(AdminRateLimit.count, AdminRateLimit.blocked_until)
            .where(AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH)).first()
        if (row is None or row.blocked_until is None or row.blocked_until <= now
                or row.count < settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS_PER_DAY):
            return 0
        return int((row.blocked_until - now).total_seconds()) + 1

    def _count_wrong_today(self, key: str, now: datetime) -> None:
        expired = or_(AdminRateLimit.blocked_until.is_(None), AdminRateLimit.blocked_until <= now)
        self.db.execute(
            pg_insert(AdminRateLimit)
            .values(key=key, window_start=_EPOCH, count=1, updated_at=now, blocked_until=now + _DAY)
            .on_conflict_do_update(
                index_elements=[AdminRateLimit.key, AdminRateLimit.window_start],
                set_={"count": case((expired, 1), else_=AdminRateLimit.count + 1), "updated_at": now,
                      "blocked_until": case((expired, now + _DAY), else_=AdminRateLimit.blocked_until)}))

    def _request_allowed(self, key: str, now: datetime) -> bool:
        """At most PASSWORD_RESET_REQUESTS_PER_HOUR emails per address per window; the window opens at the first one.
        ``blocked_until`` holds the window's end. Counts the request when it is allowed."""
        row = self.db.execute(
            select(AdminRateLimit.count, AdminRateLimit.blocked_until)
            .where(AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH)).first()
        if row is None or row.blocked_until is None or row.blocked_until <= now:
            self.db.execute(
                pg_insert(AdminRateLimit)
                .values(key=key, window_start=_EPOCH, count=1, updated_at=now, blocked_until=now + _WINDOW)
                .on_conflict_do_update(
                    index_elements=[AdminRateLimit.key, AdminRateLimit.window_start],
                    set_={"count": 1, "updated_at": now, "blocked_until": now + _WINDOW}))
            return True
        if row.count >= settings.PASSWORD_RESET_REQUESTS_PER_HOUR:
            return False
        self.db.execute(
            AdminRateLimit.__table__.update()
            .where(AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH)
            .values(count=AdminRateLimit.count + 1, updated_at=now))
        return True

    @staticmethod
    def _apply_new_password(user: User, new_password: str) -> None:
        user.hashed_password = get_password_hash(new_password)
        user.token_version = (user.token_version or 0) + 1  # ends every session issued before this reset
        user.password_reset_code_hash = None
        user.password_reset_code_expires_at = None
        user.password_reset_token = None
        user.password_reset_expires_at = None

    # --- request -----------------------------------------------------------------------------------------
    def request_code(self, email: str) -> Optional[Tuple[str, str, Optional[str]]]:
        """Issue a code for ``email`` if it belongs to an active account and the address is not over its hourly limit.

        Returns ``(email, code, legacy_token_or_None)`` for the caller to email, else None. The caller answers the same
        200 either way; a throttled request is logged (by user id when there is one)."""
        now = _now()
        ehash = _email_hash(email)
        self._lock_address(ehash)
        self._purge_stale(now)
        user = self._locked_user(email)
        if self._daily_wait(f"{_DAILY_PREFIX}{ehash}", now):
            self.db.commit()
            logger.warning("password-recovery: address locked out for the day, no code issued user=%s", user.id if user else "-")
            return None
        if not self._request_allowed(f"{_REQUEST_PREFIX}{ehash}", now):
            self.db.commit()  # releases the locks; the purge above is kept
            logger.warning("password-recovery: throttled, no code issued user=%s", user.id if user else "-")
            return None
        # a fresh request opens a fresh set of guesses, for known and unknown addresses alike
        self.db.execute(delete(AdminRateLimit).where(AdminRateLimit.key == f"{_ATTEMPT_PREFIX}{ehash}"))
        if user is None or not user.is_active:
            self.db.commit()
            return None
        code = new_code()
        user.password_reset_code_hash = hash_code(user.id, code)
        user.password_reset_code_expires_at = now + timedelta(minutes=settings.PASSWORD_RESET_CODE_EXPIRE_MINUTES)
        legacy_token = None
        if settings.PASSWORD_RESET_LEGACY_TOKEN_ENABLED:
            legacy_token = generate_password_reset_token(user.email)
            user.password_reset_token = legacy_token
            user.password_reset_expires_at = now + timedelta(hours=settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS)
        else:
            user.password_reset_token = None
            user.password_reset_expires_at = None
        self.db.commit()
        logger.info("password-recovery: code issued user=%s", user.id)
        return user.email, code, legacy_token

    # --- reset with the emailed code -------------------------------------------------------------------------
    def reset_with_code(self, email: str, code: str, new_password: str) -> User:
        """Raises WeakPassword, TooManyCodeAttempts or InvalidOrExpiredCode; commits and returns the user on success."""
        validate_new_password(new_password)  # first: depends on nothing secret, counts and consumes nothing
        now = _now()
        ehash = _email_hash(email)
        attempts_key = f"{_ATTEMPT_PREFIX}{ehash}"
        daily_key = f"{_DAILY_PREFIX}{ehash}"
        self._lock_address(ehash)
        wait = self._daily_wait(daily_key, now)
        if wait:
            self.db.commit()
            logger.warning("password-reset: daily attempts exhausted, refused")
            raise TooManyCodeAttemptsToday(wait)
        if self._wrong_attempts(attempts_key, now) >= settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS:
            self.db.commit()
            logger.warning("password-reset: attempts exhausted, refused")
            raise TooManyCodeAttempts()
        user = self._locked_user(email)
        candidate = code.strip() if isinstance(code, str) else ""
        well_formed = bool(_CODE_RE.fullmatch(candidate))
        # Always compute a hash and compare, so a hit and a miss cost the same.
        subject_id = user.id if user is not None else UUID(int=0)
        presented = hash_code(subject_id, candidate if well_formed else "-" * CODE_LENGTH)
        stored = (user.password_reset_code_hash if user is not None else None) or ""
        matches = hmac.compare_digest(presented.encode("ascii"), (stored or "0" * len(presented)).encode("ascii"))
        valid = (
            well_formed and user is not None and user.is_active and bool(stored) and matches
            and user.password_reset_code_expires_at is not None and user.password_reset_code_expires_at > now
        )
        if not valid:
            self._count_wrong_attempt(attempts_key, now)
            self._count_wrong_today(daily_key, now)
            self.db.commit()  # the count is durable before the locks are released
            logger.warning("password-reset: wrong or expired code user=%s", user.id if user else "-")
            raise InvalidOrExpiredCode()
        self._apply_new_password(user, new_password)
        self.db.execute(delete(AdminRateLimit).where(AdminRateLimit.key.in_(
            [attempts_key, daily_key, f"{_REQUEST_PREFIX}{ehash}"])))
        self.db.commit()
        logger.info("password-reset: password reset by code, other sessions ended user=%s", user.id)
        return user

    # --- deprecated: reset with the long link token ---------------------------------------------------------
    def reset_with_legacy_token(self, token: str, new_password: str) -> User:
        """The old ``{token, new_password}`` form. Same policy, same session end and the same single use as the code form."""
        validate_new_password(new_password)
        if not settings.PASSWORD_RESET_LEGACY_TOKEN_ENABLED:
            raise LegacyTokenInvalid(400, "Invalid token")
        email = verify_password_reset_token(token)
        if not email:
            raise LegacyTokenInvalid(400, "Invalid token")
        self._lock_address(_email_hash(email))
        user = self._locked_user(email)
        now = _now()
        stored = (user.password_reset_token if user is not None else None) or ""
        if user is None or not user.is_active or not stored \
                or not hmac.compare_digest(stored.encode("utf-8"), token.encode("utf-8")) \
                or user.password_reset_expires_at is None or user.password_reset_expires_at <= now:
            self.db.rollback()
            raise LegacyTokenInvalid(400, "Invalid or expired token")
        self._apply_new_password(user, new_password)
        ehash = _email_hash(email)
        self.db.execute(delete(AdminRateLimit).where(AdminRateLimit.key.in_(
            [f"{_ATTEMPT_PREFIX}{ehash}", f"{_DAILY_PREFIX}{ehash}", f"{_REQUEST_PREFIX}{ehash}"])))
        self.db.commit()
        logger.info("password-reset: password reset by legacy token, other sessions ended user=%s", user.id)
        return user
