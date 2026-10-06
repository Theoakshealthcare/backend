"""Plain factory functions for tests. Each one commits and returns the ORM object.

Usage in a test:
    def test_x(db, client):
        patient = make_patient(db)
        r = client.get("/api/v1/auth/me", headers=auth_headers(patient.user))
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.security import create_access_token, create_refresh_token, encrypt, get_password_hash
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import Caregiver, CaregiverBankAccount, CaregiverWallet
from app.db.models.patient import Patient
from app.db.models.user import Admin, User

__all__ = [
    "DEFAULT_PASSWORD",
    "auth_headers",
    "refresh_headers",
    "make_user",
    "make_patient",
    "make_caregiver",
    "make_admin",
    "make_booking",
    "make_caregiver_wallet",
]

DEFAULT_PASSWORD = "Passw0rd!test"  # placeholder test credential


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def auth_headers(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def refresh_headers(user: User) -> dict:
    """A refresh token presented as a bearer token (should be rejected by API routes)."""
    return {"Authorization": f"Bearer {create_refresh_token(user.id)}"}


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


def make_admin(db: Session) -> User:
    user = make_user(db, "patient")  # mirrors how admins exist today (an Admin row on a user)
    db.add(Admin(user_id=user.id, email=user.email, full_name=user.full_name, permissions="{}"))
    db.commit()
    return user


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
