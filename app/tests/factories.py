"""Plain factory functions for tests. Each one commits and returns the ORM object.

Usage in a test:
    def test_x(db, client):
        patient = make_patient(db)
        r = client.get("/api/v1/auth/me", headers=auth_headers(patient.user))
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple, Union

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.core.admin_security import create_admin_token, hash_token, new_opaque_token, new_totp_secret
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decrypt,
    encrypt,
    get_password_hash,
)
from app.db.models.admin import AdminRefreshToken, AdminRole, AdminUser
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import Caregiver, CaregiverBankAccount, CaregiverWallet
from app.db.models.patient import Patient
from app.db.models.user import User

__all__ = [
    "DEFAULT_PASSWORD",
    "auth_headers",
    "refresh_headers",
    "make_user",
    "make_patient",
    "make_caregiver",
    "make_admin",
    "admin_session",
    "admin_totp_secret",
    "make_booking",
    "make_caregiver_wallet",
]

DEFAULT_PASSWORD = "Passw0rd!test"  # placeholder test credential


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def auth_headers(actor: Union[User, AdminUser]) -> dict:
    """A bearer header for an app user, or (for an AdminUser) a live admin session's access token."""
    if isinstance(actor, AdminUser):
        access, _refresh = admin_session(object_session(actor), actor)
        return {"Authorization": f"Bearer {access}"}
    return {"Authorization": f"Bearer {create_access_token(actor.id)}"}


def refresh_headers(actor: Union[User, AdminUser]) -> dict:
    """A refresh token presented as a bearer token (should be rejected by API routes)."""
    if isinstance(actor, AdminUser):
        token = create_admin_token(actor.id, "refresh", timedelta(minutes=30), sid=str(uuid.uuid4()))
        return {"Authorization": f"Bearer {token}"}
    return {"Authorization": f"Bearer {create_refresh_token(actor.id)}"}


def make_user(db: Session, user_type: str = "patient", **overrides) -> User:
    tag = _unique(user_type)
    fields = dict(
        email=f"{tag}@example.com",
        phone_number=f"+2348{uuid.uuid4().int % 10**9:09d}",
        full_name=f"Test {user_type.title()}",
        hashed_password=get_password_hash(DEFAULT_PASSWORD),
        user_type=user_type,
        is_active=True,
        is_verified=True,
    )
    fields.update(overrides)
    user = User(**fields)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_patient(db: Session, **overrides) -> Patient:
    user = make_user(db, "patient")
    fields = dict(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        city="Lagos",
        state="Lagos",
    )
    fields.update(overrides)
    patient = Patient(**fields)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def make_caregiver(db: Session, approved: bool = True, **overrides) -> Caregiver:
    user = make_user(db, "caregiver")
    fields = dict(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        state="Lagos",
        specialization="nurse",
        years_of_experience=5,
        profile_completion_step=3,
        profile_status="approved" if approved else "under_review",
        identity_verification_status="verified" if approved else "pending",
    )
    fields.update(overrides)
    caregiver = Caregiver(**fields)
    db.add(caregiver)
    db.commit()
    db.refresh(caregiver)
    return caregiver


def make_admin(
    db: Session,
    roles: Sequence[str] = ("super_admin",),
    *,
    status: str = "active",
    mfa: bool = True,
    password: Optional[str] = DEFAULT_PASSWORD,
    email: Optional[str] = None,
    full_name: str = "Test Admin",
) -> AdminUser:
    """An admin_users row with the given roles (default: Super Admin, i.e. every permission).

    With ``mfa=True`` TOTP is already enrolled; read the secret with ``admin_totp_secret(admin)``.
    ``password=None`` skips the (slow) bcrypt hash for tests that never sign in with a password.
    """
    role_rows = list(db.scalars(select(AdminRole).where(AdminRole.key.in_(list(roles)))))
    assert len(role_rows) == len(set(roles)), f"unknown role in {roles}"
    admin = AdminUser(
        email=email or f"{_unique('admin')}@example.com",
        full_name=full_name,
        password_hash=get_password_hash(password) if password is not None else None,
        status=status,
        roles=role_rows,
        mfa_enabled=mfa,
        mfa_secret_enc=encrypt(new_totp_secret()) if mfa else None,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def admin_totp_secret(admin: AdminUser) -> str:
    return decrypt(admin.mfa_secret_enc)


def admin_session(db: Session, admin: AdminUser) -> Tuple[str, str]:
    """Open a session for ``admin`` without going through login. Returns (access, refresh)."""
    now = datetime.utcnow()
    family = uuid.uuid4()
    raw = new_opaque_token()
    db.add(AdminRefreshToken(
        admin_user_id=admin.id, family_id=family, token_hash=hash_token(raw),
        session_started_at=now, issued_at=now, expires_at=now + timedelta(minutes=30),
    ))
    db.commit()
    access = create_admin_token(admin.id, "access", timedelta(minutes=15), sid=str(family))
    return access, raw


def make_booking(
    db: Session,
    patient: Patient,
    caregiver: Caregiver | None = None,
    status: BookingStatus = BookingStatus.PENDING,
    payment_status: PaymentStatus = PaymentStatus.PAID,
    total_amount_kobo: int = 4_400_000,
    visit_code: str | None = None,
    **overrides,
) -> Booking:
    start = datetime.utcnow() + timedelta(days=1)
    fields = dict(
        booking_code=uuid.uuid4().hex[:8].upper(),
        patient_id=patient.id,
        caregiver_id=caregiver.id if caregiver else None,
        service_type="nurse",
        assistance_type="home_visit",
        duration_hours=4,
        scheduled_date=start,
        scheduled_end_date=start + timedelta(hours=4),
        patient_address="1 Test Close, Lekki",
        patient_latitude=6.4474,
        patient_longitude=3.4723,
        patient_city="Lagos",
        patient_state="Lagos",
        total_amount_kobo=total_amount_kobo,
        service_fee_kobo=round(total_amount_kobo / 1.1),
        platform_fee_kobo=total_amount_kobo - round(total_amount_kobo / 1.1),
        booking_for="myself",
        status=status,
        payment_status=payment_status,
        paystack_reference=f"BKG_{uuid.uuid4().hex[:12]}",
    )
    if visit_code is not None:
        fields["visit_code_enc"] = encrypt(visit_code)
    fields.update(overrides)
    booking = Booking(**fields)
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return booking


def make_caregiver_wallet(db: Session, caregiver: Caregiver, balance_kobo: int = 0, with_bank: bool = True):
    wallet = CaregiverWallet(caregiver_id=caregiver.id, balance=balance_kobo)
    db.add(wallet)
    if with_bank:
        db.add(
            CaregiverBankAccount(
                caregiver_id=caregiver.id,
                bank_name="Test Bank",
                account_number="0000000000",  # placeholder
                account_name=caregiver.full_name,
            )
        )
    db.commit()
    db.refresh(wallet)
    return wallet
