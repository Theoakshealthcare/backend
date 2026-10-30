"""Change an app user's password (POST /auth/change-password).

The caller proves the current password, the new one must pass ``validate_new_password``, and every token issued before
the change stops working (``users.token_version``). Wrong current passwords are counted per user in ``admin_rate_limits``
(the key is ``cpw:<user id>``; that table is a generic counter store despite its name) and, past a few free attempts,
every call is answered 429 for a wait that doubles each time. Weak or unchanged new passwords are not guesses and are not counted.
"""
import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import BCRYPT_MAX_BYTES, get_password_hash, verify_password
from app.db.models.admin import AdminRateLimit
from app.db.models.user import User

logger = logging.getLogger(__name__)

PASSWORD_MIN_LENGTH = 8  # what the app's register and reset screens already require
PASSWORD_MIN_DISTINCT = 5
_EPOCH = datetime(1970, 1, 1)  # window_start of a backoff row (the admin limiter's convention)


class ChangePasswordError(Exception):
    status_code = 400
    code = ""
    message = ""


class WrongCurrentPassword(ChangePasswordError):
    code = "wrong_current_password"
    message = "Your current password is incorrect."


class WeakPassword(ChangePasswordError):
    code = "weak_password"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SamePassword(ChangePasswordError):
    code = "same_password"
    message = "Choose a password you are not using now."


class TooManyAttempts(ChangePasswordError):
    status_code = 429
    code = "too_many_attempts"

    def __init__(self, retry_after: int):
        super().__init__(retry_after)
        self.retry_after = retry_after
        self.message = "Too many incorrect attempts. Please wait before trying again."


def validate_new_password(password: str) -> None:
    """Raises WeakPassword. Seen from outside the 72-byte cut: bcrypt ignores everything after it, so a longer
    password would silently be a shorter one."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise WeakPassword(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise WeakPassword(f"Password must be at most {BCRYPT_MAX_BYTES} bytes.")
    if len(set(password)) < PASSWORD_MIN_DISTINCT:
        raise WeakPassword("Password is too repetitive.")


class ChangePasswordService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _key(user_id: UUID) -> str:
        return f"cpw:{user_id}"

    def change(self, user_id: UUID, current_password: str, new_password: str) -> User:
        """Commits and returns the user (its ``token_version`` is the new one). Raises a ChangePasswordError."""
        key = self._key(user_id)
        now = datetime.utcnow()
        self._raise_if_throttled(key, now)
        # Serialise two changes for the same user. The auth dependency already loaded this row into the session, so
        # populate_existing makes the second change see the hash and version the first one committed, not stale ones.
        user = self.db.execute(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        ).scalar_one()
        if not verify_password(current_password, user.hashed_password):
            self.db.rollback()  # release the row lock before counting the failure
            self._register_failure(key, now)
            logger.warning("change-password: wrong current password user=%s", user_id)
            raise WrongCurrentPassword()
        validate_new_password(new_password)
        if verify_password(new_password, user.hashed_password):
            raise SamePassword()
        user.hashed_password = get_password_hash(new_password)
        user.token_version = (user.token_version or 0) + 1
        user.password_reset_token = None  # a reset link emailed earlier must not undo this change
        user.password_reset_expires_at = None
        self.db.execute(delete(AdminRateLimit).where(AdminRateLimit.key == key))
        self.db.commit()
        self.db.refresh(user)
        logger.info("change-password: password changed, other sessions ended user=%s", user_id)
        return user

    def _raise_if_throttled(self, key: str, now: datetime) -> None:
        blocked_until = self.db.scalar(
            select(AdminRateLimit.blocked_until).where(AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH))
        if blocked_until is not None and blocked_until > now:
            logger.warning("change-password: throttled user key=%s", key)
            raise TooManyAttempts(int((blocked_until - now).total_seconds()) + 1)

    def _register_failure(self, key: str, now: datetime) -> None:
        """Count one wrong current password; past the free attempts start or extend the wait. A row nobody touched for
        an hour starts again at one."""
        stale = AdminRateLimit.updated_at < now - timedelta(hours=1)
        count = self.db.execute(
            pg_insert(AdminRateLimit)
            .values(key=key, window_start=_EPOCH, count=1, updated_at=now)
            .on_conflict_do_update(
                index_elements=[AdminRateLimit.key, AdminRateLimit.window_start],
                set_={"count": case((stale, 1), else_=AdminRateLimit.count + 1),
                      "updated_at": now,
                      "blocked_until": case((stale, None), else_=AdminRateLimit.blocked_until)},
            )
            .returning(AdminRateLimit.count)
        ).scalar_one()
        over = count - settings.CHANGE_PASSWORD_FREE_ATTEMPTS
        if over >= 0:
            wait = min(settings.CHANGE_PASSWORD_BACKOFF_SECONDS * (2 ** min(over, 20)),
                       settings.CHANGE_PASSWORD_BACKOFF_MAX_SECONDS)
            self.db.execute(
                update(AdminRateLimit)
                .where(AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH)
                .values(blocked_until=now + timedelta(seconds=wait)))
        self.db.commit()
