"""Admin sign-in: password, TOTP MFA, rotating refresh tokens, throttling, lockout, IP limits (AD-FR-81).

Two different defences, on purpose:
- password stage: throttled per (email, client IP) with a growing wait, never a hard lock, and
  identical for known and unknown emails (a hard lock here would let anyone lock an admin out);
- MFA stage (the password is already proven): the account locks, for longer each time.

Every method commits its own work, because the counters it keeps (failed logins, rate limits,
revoked sessions) must survive the request failing with a 4xx.
"""

import logging
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import Request
from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.admin_security import (
    AdminTokenError,
    create_admin_token,
    decode_admin_token,
    hash_token,
    matching_totp_step,
    new_opaque_token,
    new_totp_secret,
    provisioning_uri,
)
from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.security import decrypt, encrypt, get_password_hash, verify_password
from app.db.models.admin import AdminRateLimit, AdminRefreshToken, AdminUser
from app.services.audit import AuditService

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1)
_dummy_hash: Optional[str] = None


def _now_ts() -> float:
    """The clock the TOTP check reads. Tests replace it to step through 30-second windows."""
    return time.time()


class AdminAuthError(Exception):
    """Base of the errors the endpoints translate into HTTP responses."""


class InvalidCredentials(AdminAuthError):
    pass


class InvalidMfaToken(AdminAuthError):
    pass


class InvalidMfaCode(AdminAuthError):
    pass


class InvalidRefreshToken(AdminAuthError):
    pass


class InvalidInvite(AdminAuthError):
    pass


class WeakPassword(AdminAuthError):
    pass


class AccountLocked(AdminAuthError):
    def __init__(self, retry_after: int):
        super().__init__("Account temporarily locked")
        self.retry_after = retry_after


class RateLimited(AdminAuthError):
    def __init__(self, retry_after: int):
        super().__init__("Too many requests")
        self.retry_after = retry_after


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str, email: str = "") -> None:
    """Raises ValueError with a human message. Shared by invite accept and the bootstrap CLI."""
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    if len(password.encode()) > 72:
        raise ValueError("Password must be at most 72 bytes")
    if len(set(password)) < 5:
        raise ValueError("Password is too repetitive")
    local = email.split("@")[0].lower() if email else ""
    if local and len(local) >= 4 and local in password.lower():
        raise ValueError("Password must not contain your email name")


def _check_password(password_hash: Optional[str], password: str) -> bool:
    """Always spends one bcrypt verification, so an unknown email costs the same as a wrong password."""
    global _dummy_hash
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = get_password_hash(uuid.uuid4().hex)
        verify_password(password, _dummy_hash)
        return False
    return verify_password(password, password_hash)


class AdminAuthService:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    # --- rate limiting -------------------------------------------------------------------------

    def rate_limit(self, bucket: str, subject: str) -> None:
        """Count one call from ``subject`` (a client IP, or a token hash) in ``bucket``.

        Raises RateLimited past the per-window limit.
        """
        window = settings.ADMIN_AUTH_IP_WINDOW_SECONDS
        now = datetime.utcnow()
        secs = int((now - _EPOCH).total_seconds())
        start = _EPOCH + timedelta(seconds=secs - secs % window)
        stmt = (
            pg_insert(AdminRateLimit)
            .values(key=f"{bucket}:{subject}", window_start=start, count=1, updated_at=now)
            .on_conflict_do_update(
                index_elements=[AdminRateLimit.key, AdminRateLimit.window_start],
                set_={"count": AdminRateLimit.count + 1, "updated_at": now},
            )
            .returning(AdminRateLimit.count)
        )
        count = self.db.execute(stmt).scalar_one()
        self._housekeeping(now)
        self.db.commit()
        if count > settings.ADMIN_AUTH_IP_LIMIT:
            retry = int((start + timedelta(seconds=window) - now).total_seconds()) + 1
            raise RateLimited(max(retry, 1))

    def _housekeeping(self, now: datetime) -> None:
        if random.random() < 0.02:  # old counters are useless
            self.db.execute(delete(AdminRateLimit).where(AdminRateLimit.updated_at < now - timedelta(days=1)))

    @staticmethod
    def _password_key(email: str, ip: str) -> str:
        return f"pw:{hash_token(normalize_email(email))[:32]}:{ip}"

    def _raise_if_password_throttled(self, key: str, now: datetime) -> None:
        blocked_until = self.db.scalar(
            select(AdminRateLimit.blocked_until).where(
                AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH)
        )
        if blocked_until is not None and blocked_until > now:
            raise RateLimited(int((blocked_until - now).total_seconds()) + 1)

    def _register_password_failure(self, key: str, now: datetime) -> None:
        """Count one failed password for (email, IP); past the free attempts, start/extend the wait.

        Called for every failure alike (unknown, invited, disabled, wrong password), so the
        counter cannot tell the cases apart.
        """
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
        over = count - settings.ADMIN_PASSWORD_FREE_ATTEMPTS
        if over >= 0:
            wait = min(settings.ADMIN_PASSWORD_BACKOFF_SECONDS * (2 ** min(over, 20)),
                       settings.ADMIN_PASSWORD_BACKOFF_MAX_SECONDS)
            self.db.execute(
                update(AdminRateLimit)
                .where(AdminRateLimit.key == key, AdminRateLimit.window_start == _EPOCH)
                .values(blocked_until=now + timedelta(seconds=wait))
            )
        self._housekeeping(now)
        self.db.commit()

    def _clear_password_failures(self, key: str) -> None:
        self.db.execute(delete(AdminRateLimit).where(AdminRateLimit.key == key))

    # --- login ---------------------------------------------------------------------------------

    def login(self, email: str, password: str, request: Request) -> Dict[str, Any]:
        ip = client_ip(request)
        self.rate_limit("auth", f"ip:{ip}")
        pw_key = self._password_key(email, ip)
        now = datetime.utcnow()
        self._raise_if_password_throttled(pw_key, now)

        # No row lock here: holding one across a bcrypt check would let anyone queue up behind it.
        admin = self.db.scalar(select(AdminUser).where(AdminUser.email == normalize_email(email)))
        password_ok = _check_password(admin.password_hash if admin else None, password)
        if admin is None or not password_ok or admin.status != "active":
            # unknown, invited, disabled and wrong password all take this one path
            self._register_password_failure(pw_key, now)
            raise InvalidCredentials()

        # The password is proven. Only now can the answer depend on the account's state.
        self._clear_password_failures(pw_key)
        self._raise_if_locked(admin, now)

        mfa_token = create_admin_token(
            admin.id, "mfa", timedelta(minutes=settings.ADMIN_MFA_TOKEN_MINUTES)
        )
        if admin.mfa_enabled and admin.mfa_secret_enc:
            self.db.commit()  # the cleared throttle
            return {"mfa_required": True, "mfa_token": mfa_token}

        # Not enrolled yet: hand out (or re-hand out) the pending secret to enrol with.
        admin = self.db.scalar(
            select(AdminUser).where(AdminUser.id == admin.id).with_for_update().execution_options(populate_existing=True)
        )
        if not admin.mfa_secret_enc:
            admin.mfa_secret_enc = encrypt(new_totp_secret())
        self.db.commit()
        secret = decrypt(admin.mfa_secret_enc)
        return {
            "mfa_enrollment_required": True,
            "mfa_token": mfa_token,
            "otpauth_uri": provisioning_uri(secret, admin.email),
            "secret": secret,
        }

    def verify_mfa(self, mfa_token: str, code: str, request: Request) -> Dict[str, Any]:
        self.rate_limit("auth", f"ip:{client_ip(request)}")
        try:
            claims = decode_admin_token(mfa_token, "mfa")
            admin_id = uuid.UUID(claims["sub"])
            jti = str(claims["jti"])
        except (AdminTokenError, ValueError, KeyError):
            raise InvalidMfaToken()
        jti_key = f"mfajti:{jti}"
        admin = self.db.scalar(select(AdminUser).where(AdminUser.id == admin_id).with_for_update().execution_options(populate_existing=True))
        if admin is None or admin.status != "active" or not admin.mfa_secret_enc:
            raise InvalidMfaToken()
        # Checked under the row lock, so a thread that waited behind a sign-in using this same
        # token sees the marker that sign-in committed.
        if self.db.scalar(select(AdminRateLimit.key).where(AdminRateLimit.key == jti_key)) is not None:
            self.db.rollback()
            raise InvalidMfaToken()  # already used to sign in; not a code guess, so no failure counted

        now = datetime.utcnow()
        self._raise_if_locked(admin, now)
        step = matching_totp_step(decrypt(admin.mfa_secret_enc), code, _now_ts())
        # A code from a step at or before the last accepted one is a replay (the row is locked, so
        # two simultaneous submissions of the same code cannot both pass).
        if step is None or (admin.mfa_last_step is not None and step <= admin.mfa_last_step):
            self._register_failure(admin, request)
            raise InvalidMfaCode()

        # Spend the token. The admin row is locked, so two threads holding the same token are
        # serialised here and the second finds the marker.
        spent = self.db.execute(
            pg_insert(AdminRateLimit)
            .values(key=jti_key, window_start=_EPOCH, count=1, updated_at=now)
            .on_conflict_do_nothing()
            .returning(AdminRateLimit.key)
        ).first()
        if spent is None:
            self.db.rollback()
            raise InvalidMfaToken()

        if not admin.mfa_enabled:
            admin.mfa_enabled = True
            self.audit.record(admin_user_id=admin.id, action="admin.mfa_enrolled",
                              entity_type="admin_user", entity_id=admin.id, request=request)
        admin.mfa_last_step = step
        admin.failed_logins = 0
        admin.mfa_lock_count = 0
        admin.locked_until = None
        admin.last_login_at = now
        tokens = self._start_session(admin, now)
        self.audit.record(admin_user_id=admin.id, action="admin.login",
                          entity_type="admin_user", entity_id=admin.id, request=request)
        self.db.commit()
        return {**tokens, "admin": admin}

    # --- sessions ------------------------------------------------------------------------------

    def refresh(self, refresh_token: str, request: Request) -> Dict[str, Any]:
        # Not in the per-IP bucket: the token is a 256-bit secret, so guessing is hopeless and
        # sharing the login bucket would let login noise lock admins out of their sessions.
        # A token that exists is limited by its own hash (junk tokens leave no rows).
        token_hash = hash_token(refresh_token)
        if self.db.scalar(select(AdminRefreshToken.id).where(AdminRefreshToken.token_hash == token_hash)) is None:
            raise InvalidRefreshToken()
        self.rate_limit("refresh", f"tok:{token_hash}")
        row = self.db.scalar(
            select(AdminRefreshToken)
            .where(AdminRefreshToken.token_hash == token_hash)
            .with_for_update().execution_options(populate_existing=True)
        )
        if row is None:
            raise InvalidRefreshToken()
        now = datetime.utcnow()

        if row.revoked_at is not None:
            if row.replaced_by_id is not None:
                # A token that was already rotated is being presented again: either the client
                # raced itself or the token was stolen. Cut every session of this admin.
                self._revoke_all_sessions(row.admin_user_id, now)
                self.audit.record(admin_user_id=row.admin_user_id, action="admin.refresh_token_reuse",
                                  entity_type="admin_user", entity_id=row.admin_user_id, request=request)
                self.db.commit()
                log.warning("Admin refresh token reuse detected; all sessions revoked (admin %s)",
                            row.admin_user_id)
            raise InvalidRefreshToken()

        max_end = row.session_started_at + timedelta(hours=settings.ADMIN_SESSION_MAX_HOURS)
        if row.expires_at <= now or max_end <= now:
            self._revoke_family(row.family_id, now)
            self.db.commit()
            raise InvalidRefreshToken()

        admin = self.db.get(AdminUser, row.admin_user_id)
        if admin is None or admin.status != "active":
            self._revoke_family(row.family_id, now)
            self.db.commit()
            raise InvalidRefreshToken()

        raw, new_row = self._new_refresh_row(admin.id, row.family_id, row.session_started_at, now)
        self.db.add(new_row)
        self.db.flush()
        row.revoked_at = now
        row.replaced_by_id = new_row.id
        self.db.commit()
        return self._token_pair(admin.id, row.family_id, raw)

    def logout(self, refresh_token: str) -> None:
        # FOR UPDATE: a refresh in flight holds this row until it commits, so logout waits for it
        # and its UPDATE then also sees (and revokes) the token that refresh just issued.
        row = self.db.scalar(
            select(AdminRefreshToken)
            .where(AdminRefreshToken.token_hash == hash_token(refresh_token))
            .with_for_update().execution_options(populate_existing=True)
        )
        if row is not None:
            self._revoke_family(row.family_id, datetime.utcnow())
        self.db.commit()

    # --- invites -------------------------------------------------------------------------------

    def accept_invite(self, invite_token: str, password: str, request: Request) -> AdminUser:
        self.rate_limit("invite", f"ip:{client_ip(request)}")
        admin = self.db.scalar(
            select(AdminUser)
            .where(AdminUser.invite_token_hash == hash_token(invite_token), AdminUser.status == "invited")
            .with_for_update().execution_options(populate_existing=True)
        )
        now = datetime.utcnow()
        if admin is None or admin.invite_expires_at is None or admin.invite_expires_at <= now:
            raise InvalidInvite()
        try:
            validate_password(password, admin.email)
        except ValueError as exc:
            self.db.rollback()
            raise WeakPassword(str(exc)) from exc
        admin.password_hash = get_password_hash(password)
        admin.status = "active"
        admin.invite_token_hash = None  # single use
        admin.invite_expires_at = None
        self.audit.record(admin_user_id=admin.id, action="admin.invite_accepted",
                          entity_type="admin_user", entity_id=admin.id, request=request)
        self.db.commit()
        return admin

    # --- internals -----------------------------------------------------------------------------

    def _raise_if_locked(self, admin: AdminUser, now: datetime) -> None:
        if admin.locked_until is not None and admin.locked_until > now:
            retry = int((admin.locked_until - now).total_seconds()) + 1
            self.db.rollback()
            raise AccountLocked(retry)

    def _register_failure(self, admin: AdminUser, request: Request) -> None:
        """A wrong TOTP code. The Nth lock in a row lasts longer than the one before."""
        admin.failed_logins = (admin.failed_logins or 0) + 1
        if admin.failed_logins >= settings.ADMIN_MAX_FAILED_LOGINS:
            tiers = settings.ADMIN_LOCKOUT_TIERS_MINUTES
            minutes = tiers[min(admin.mfa_lock_count or 0, len(tiers) - 1)]
            admin.mfa_lock_count = (admin.mfa_lock_count or 0) + 1
            admin.failed_logins = 0
            admin.locked_until = datetime.utcnow() + timedelta(minutes=minutes)
            self.audit.record(admin_user_id=admin.id, action="admin.locked", entity_type="admin_user",
                              entity_id=admin.id, after={"stage": "mfa", "lock_count": admin.mfa_lock_count,
                                                         "minutes": minutes}, request=request)
            log.warning("Admin account locked for %d min after repeated MFA failures (admin %s, lock #%d)",
                        minutes, admin.id, admin.mfa_lock_count)
        self.db.commit()

    def _new_refresh_row(self, admin_id, family_id, session_started_at, now):
        raw = new_opaque_token()
        expires = now + timedelta(minutes=settings.ADMIN_IDLE_TIMEOUT_MINUTES)
        expires = min(expires, session_started_at + timedelta(hours=settings.ADMIN_SESSION_MAX_HOURS))
        row = AdminRefreshToken(
            id=uuid.uuid4(), admin_user_id=admin_id, family_id=family_id, token_hash=hash_token(raw),
            session_started_at=session_started_at, issued_at=now, expires_at=expires,
        )
        return raw, row

    def _token_pair(self, admin_id, family_id, raw_refresh: str) -> Dict[str, Any]:
        access = create_admin_token(
            admin_id, "access", timedelta(minutes=settings.ADMIN_ACCESS_TOKEN_MINUTES), sid=str(family_id)
        )
        return {
            "access_token": access,
            "refresh_token": raw_refresh,
            "token_type": "bearer",
            "expires_in": settings.ADMIN_ACCESS_TOKEN_MINUTES * 60,
        }

    def _start_session(self, admin: AdminUser, now: datetime) -> Dict[str, Any]:
        family_id = uuid.uuid4()
        raw, row = self._new_refresh_row(admin.id, family_id, now, now)
        self.db.add(row)
        self.db.flush()
        return self._token_pair(admin.id, family_id, raw)

    def _revoke_family(self, family_id, now: datetime) -> None:
        self.db.execute(
            update(AdminRefreshToken)
            .where(AdminRefreshToken.family_id == family_id, AdminRefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    def _revoke_all_sessions(self, admin_id, now: datetime) -> None:
        self.db.execute(
            update(AdminRefreshToken)
            .where(AdminRefreshToken.admin_user_id == admin_id, AdminRefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )


def revoke_all_sessions(db: Session, admin_id) -> None:
    """Used by admin management (disable, MFA reset). The caller commits."""
    AdminAuthService(db)._revoke_all_sessions(admin_id, datetime.utcnow())
