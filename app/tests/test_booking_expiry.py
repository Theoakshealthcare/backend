"""E3: unpaid bookings expire after PAYMENT_LINK_TTL_MINUTES (job body called directly, no Redis)."""

import logging
from datetime import datetime, timedelta

from app.core.config import settings
from app.db.models.booking import Booking, BookingNotification, BookingStatus, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.schemas.booking import BookingCreate
from app.services.booking import BookingService
from app.services.booking_expiry import expire_unpaid_bookings
from app.services.booking_payment import SettleOutcome, settle_booking_payment
from app.services.paystack import PaystackService
from app.tests.factories import make_booking, make_patient

NOW = datetime(2026, 10, 5, 12, 0, 0)


def _unpaid(db, expires_at, **kw):
    return make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING,
                        payment_expires_at=expires_at, **kw)


def _fresh(db, b):
    db.expire_all()
    return db.get(Booking, b.id)


def test_expired_unpaid_booking_is_cancelled_with_reason(db):
    b = _unpaid(db, NOW - timedelta(minutes=1))
    assert expire_unpaid_bookings(db, now=NOW) == 1
    b = _fresh(db, b)
    assert b.status == BookingStatus.CANCELLED
    assert b.cancel_reason == "unpaid_timeout"
    assert b.payment_status == PaymentStatus.PENDING


def test_unexpired_unpaid_booking_is_left_alone(db):
    b = _unpaid(db, NOW + timedelta(minutes=5))
    assert expire_unpaid_bookings(db, now=NOW) == 0
    assert _fresh(db, b).status == BookingStatus.PENDING


def test_paid_booking_past_expiry_is_not_cancelled(db):
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PAID,
                     payment_expires_at=NOW - timedelta(hours=1))
    assert expire_unpaid_bookings(db, now=NOW) == 0
    b = _fresh(db, b)
    assert b.status == BookingStatus.PENDING and b.cancel_reason is None


def test_only_pending_status_is_expired(db):
    b = _unpaid(db, NOW - timedelta(hours=1), status=BookingStatus.DECLINED)
    assert expire_unpaid_bookings(db, now=NOW) == 0
    assert _fresh(db, b).status == BookingStatus.DECLINED


def test_running_twice_is_idempotent(db):
    b = _unpaid(db, NOW - timedelta(minutes=1))
    assert expire_unpaid_bookings(db, now=NOW) == 1
    updated_at = _fresh(db, b).updated_at
    assert expire_unpaid_bookings(db, now=NOW + timedelta(minutes=5)) == 0
    b = _fresh(db, b)
    assert b.status == BookingStatus.CANCELLED and b.cancel_reason == "unpaid_timeout"
    assert b.updated_at == updated_at


def test_legacy_booking_without_expiry_uses_created_at_plus_ttl(db):
    old = _unpaid(db, None, created_at=NOW - timedelta(minutes=settings.PAYMENT_LINK_TTL_MINUTES + 1))
    young = _unpaid(db, None, created_at=NOW - timedelta(minutes=5))
    assert expire_unpaid_bookings(db, now=NOW) == 1
    assert _fresh(db, old).status == BookingStatus.CANCELLED
    assert _fresh(db, young).status == BookingStatus.PENDING


def test_the_old_seven_day_job_is_gone():
    assert not hasattr(BookingService, "auto_cancel_unpaid_bookings")


def test_late_payment_after_expiry_is_recorded_but_not_dispatched(db, caplog):
    b = _unpaid(db, NOW - timedelta(minutes=1), total_amount_kobo=4_400_000)
    assert expire_unpaid_bookings(db, now=NOW) == 1

    with caplog.at_level(logging.ERROR):
        outcome = settle_booking_payment(db, b.id, 4_400_000)
    assert outcome == SettleOutcome.PAID_NOT_DISPATCHED
    b = _fresh(db, b)
    assert b.payment_status == PaymentStatus.PAID
    assert b.status == BookingStatus.CANCELLED
    assert b.dispatch_state is None and b.dispatch_started_at is None
    assert db.query(BookingNotification).count() == 0
    assert "needs a refund review" in caplog.text


def test_create_booking_sets_payment_expiry(db, monkeypatch):
    db.add(BookingSettings(base_fee_kobo=500_000, platform_fee_percent=10.0))
    db.commit()
    monkeypatch.setattr(
        PaystackService, "initialize_transaction",
        lambda self, email, amount_kobo, reference, callback_url=None, metadata=None:
        {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference})
    patient = make_patient(db)
    before = datetime.utcnow()
    result = BookingService(db).create_booking(patient.id, BookingCreate(
        service_type="nurse", assistance_type="x", duration_hours=2, scheduled_date="2030-01-01T10:00:00",
        patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4,
        patient_city="Lagos", patient_state="Lagos", booking_for="myself"))
    ttl = timedelta(minutes=settings.PAYMENT_LINK_TTL_MINUTES)
    assert settings.PAYMENT_LINK_TTL_MINUTES == 30
    expires = result["booking"].payment_expires_at
    assert before + ttl <= expires <= datetime.utcnow() + ttl
