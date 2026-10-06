"""Admin identity: accounts, roles, sessions and auth rate-limit counters (AD-FR-81..84).

Admin accounts are NOT ``users`` rows. They have their own table, their own tokens (aud="admin")
and their own permission model.
"""

import uuid
from datetime import datetime
from typing import List, Set

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

ADMIN_STATUSES = ("invited", "active", "disabled")


class AdminRole(Base):
    __tablename__ = "admin_roles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String, nullable=False, unique=True)  # e.g. "super_admin"
    name = Column(String, nullable=False)  # e.g. "Super Admin"

    permission_rows = relationship("AdminRolePermission", lazy="selectin", cascade="all, delete-orphan")


class AdminRolePermission(Base):
    __tablename__ = "admin_role_permissions"

    role_id = Column(UUID(as_uuid=True), ForeignKey("admin_roles.id", ondelete="CASCADE"), primary_key=True)
    permission = Column(String, primary_key=True)  # e.g. "caregivers.approve"


class AdminUserRole(Base):
    __tablename__ = "admin_user_roles"

    admin_user_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True)
    role_id = Column(UUID(as_uuid=True), ForeignKey("admin_roles.id"), primary_key=True)


class AdminUser(Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint("status IN ('invited','active','disabled')", name="ck_admin_users_status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, nullable=False, unique=True)  # stored lower-case
    full_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=True)  # null until the invite is accepted
    status = Column(String, nullable=False, default="invited")
    # TOTP. The secret is Fernet-encrypted. It is written at first login (pending) and
    # mfa_enabled flips to true on the first successful verify.
    mfa_secret_enc = Column(String, nullable=True)
    mfa_enabled = Column(Boolean, nullable=False, default=False)
    # The last TOTP time step accepted for this admin; a code from this step or an earlier one is
    # refused, so a captured code cannot be replayed inside its 30-second window.
    mfa_last_step = Column(BigInteger, nullable=True)
    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    # Single-use invite: only the hash is stored, and it is cleared when the invite is accepted.
    invite_token_hash = Column(String, nullable=True, unique=True)
    invite_expires_at = Column(DateTime, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    roles = relationship("AdminRole", secondary="admin_user_roles", lazy="selectin")

    @property
    def role_keys(self) -> List[str]:
        return sorted(r.key for r in self.roles)

    @property
    def permissions(self) -> Set[str]:
        return {p.permission for r in self.roles for p in r.permission_rows}

    @property
    def permission_list(self) -> List[str]:
        return sorted(self.permissions)

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


class AdminRefreshToken(Base):
    """One row per issued refresh token. Rows of one login session share a ``family_id``.

    Only the SHA-256 of the token is stored. Using a token rotates it (``replaced_by_id`` set and
    ``revoked_at`` stamped); presenting a rotated token again is treated as theft.
    """
    __tablename__ = "admin_refresh_tokens"
    __table_args__ = (Index("ix_admin_refresh_tokens_family", "family_id"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_user_id = Column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id = Column(UUID(as_uuid=True), nullable=False)  # the login session; also the access token's `sid`
    token_hash = Column(String, nullable=False, unique=True)
    session_started_at = Column(DateTime, nullable=False)  # when the family began (absolute lifetime cap)
    issued_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)  # idle timeout: issued_at + 30 minutes
    revoked_at = Column(DateTime, nullable=True)
    replaced_by_id = Column(UUID(as_uuid=True), ForeignKey("admin_refresh_tokens.id", ondelete="SET NULL"), nullable=True)


class AdminRateLimit(Base):
    """Fixed-window counters for the admin auth endpoints, kept in Postgres (Redis is optional)."""
    __tablename__ = "admin_rate_limits"
    __table_args__ = (Index("ix_admin_rate_limits_window_start", "window_start"),)

    key = Column(String, primary_key=True)  # e.g. "login:ip:203.0.113.7"
    window_start = Column(DateTime, primary_key=True)
    count = Column(Integer, nullable=False, default=0)
