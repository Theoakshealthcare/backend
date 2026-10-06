"""A phone-only (WhatsApp) patient has no email: response models allow that, and Paystack gets a
deterministic non-deliverable address instead of a crash."""

import uuid

import pytest

from app.core.phone import contact_ref
from app.db.models.booking_settings import BookingSettings
from app.schemas.booking import BookingCreate
from app.schemas.patient import Patient as PatientOut
from app.schemas.patient import PatientCreate
from app.schemas.user import UserProfileResponse
from app.services.booking import BookingService
from app.services.paystack import PaystackService, paystack_email_for
from app.tests.factories import make_patient


class _Who:
    def __init__(self, email, phone_number):
        self.email, self.phone_number = email, phone_number


def test_a_real_email_is_used_as_is():
    assert paystack_email_for(_Who("a@example.com", "+2348012345678")) == "a@example.com"


def test_a_phone_only_user_gets_a_stable_non_deliverable_address():
    first = paystack_email_for(_Who(None, "+2348012345678"))
    assert first == f"wa-{contact_ref('2348012345678')}@pay.oaklycare.com"
    assert first == paystack_email_for(_Who("", "2348012345678"))  # any spelling of the same number
    assert "2348012345678" not in first  # the number itself never leaves the building


def test_a_user_with_neither_email_nor_phone_is_an_error():
    with pytest.raises(ValueError):
        paystack_email_for(_Who(None, None))


def test_response_models_accept_a_missing_email_but_registration_still_requires_one():
    out = PatientOut(id=uuid.uuid4(), user_id=uuid.uuid4(), email=None, full_name="Tolu", is_active=True,
                     profile_completion_step=0, created_at="2030-01-01T00:00:00", updated_at="2030-01-01T00:00:00")
    assert out.email is None
    profile = UserProfileResponse(id=uuid.uuid4(), full_name="Tolu", email=None, phone_number="+2348012345678",
                                  profile_image_url=None, user_type="patient", address=None, gender=None,
                                  date_of_birth=None, emergency_contact_name=None, emergency_contact_phone=None,
                                  emergency_contact_address=None, created_at=None, updated_at=None)
    assert profile.email is None
    with pytest.raises(ValueError):
        PatientCreate(user_id=uuid.uuid4(), full_name="Tolu")  # the create schema still needs an email


def _phone_only_patient(db):
    patient = make_patient(db, email=None, phone_number="+2348012345678")
    patient.user.email = None
    db.commit()
    return patient


def test_booking_creation_for_a_phone_only_patient_initialises_paystack_with_the_derived_address(db, monkeypatch):
    db.add(BookingSettings(base_fee_kobo=500_000, platform_fee_percent=10.0))
    db.commit()
    seen = {}

    def fake_init(self, email, amount_kobo, reference, callback_url=None, metadata=None):
        seen["email"] = email
        return {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference}

    monkeypatch.setattr(PaystackService, "initialize_transaction", fake_init)
    patient = _phone_only_patient(db)
    BookingService(db).create_booking(patient.id, BookingCreate(
        service_type="nurse", assistance_type="x", duration_hours=2, scheduled_date="2030-01-01T10:00:00",
        patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4,
        patient_city="Lagos", patient_state="Lagos", booking_for="myself"))
    assert seen["email"] == f"wa-{contact_ref('2348012345678')}@pay.oaklycare.com"


def test_wallet_deposit_for_a_phone_only_patient_uses_the_derived_address(db, monkeypatch):
    from app.schemas.patient import PatientDepositRequest
    from app.services.patient_wallet import PatientWalletService

    seen = {}

    def fake_init(self, email, amount_kobo, reference, callback_url=None, metadata=None):
        seen["email"] = email
        return {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference}

    monkeypatch.setattr(PaystackService, "initialize_transaction", fake_init)
    patient = _phone_only_patient(db)
    PatientWalletService(db).initiate_deposit(patient.id, PatientDepositRequest(amount="1000"))
    assert seen["email"] == f"wa-{contact_ref('2348012345678')}@pay.oaklycare.com"
