"""E3: unpaid bookings expire after PAYMENT_LINK_TTL_MINUTES (job body called directly, no Redis)."""

import logging
from datetime import datetime, timedelta

from app.core.config import settings
from app.db.models.booking import Booking, BookingNotification, BookingStatus, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.schemas.booking import BookingCreate
from app.services import dispatch
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


def _expired_and_cancelled(db, scheduled_in, total=4_400_000):
    start = NOW + scheduled_in
    b = _unpaid(db, NOW - timedelta(minutes=1), total_amount_kobo=total,
                scheduled_date=start, scheduled_end_date=start + timedelta(hours=4))
    assert expire_unpaid_bookings(db, now=NOW) == 1
    return b


def test_late_payment_with_lead_time_reinstates_and_dispatches_once(db, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: calls.append(booking_id))
    b = _expired_and_cancelled(db, timedelta(days=1))

    assert settle_booking_payment(db, b.id, 4_400_000, now=NOW) == SettleOutcome.PAID
    b = _fresh(db, b)
    assert b.payment_status == PaymentStatus.PAID
    assert b.status == BookingStatus.PENDING and b.cancel_reason is None
    assert b.dispatch_state == "dispatching" and b.dispatch_started_at == NOW
    assert b.refund_required is False

    assert settle_booking_payment(db, b.id, 4_400_000, now=NOW) == SettleOutcome.ALREADY_PAID
    assert calls == [b.id]


def test_late_payment_too_close_to_the_visit_is_marked_for_refund(db, caplog):
    b = _expired_and_cancelled(db, timedelta(minutes=settings.LATE_PAYMENT_REINSTATE_MIN_LEAD_MINUTES - 1))
    with caplog.at_level(logging.ERROR):
        outcome = settle_booking_payment(db, b.id, 4_400_000, now=NOW)
    assert outcome == SettleOutcome.PAID_NOT_DISPATCHED
    b = _fresh(db, b)
    assert b.payment_status == PaymentStatus.PAID and b.status == BookingStatus.CANCELLED
    assert b.refund_required is True and b.refund_required_reason == "paid_after_expiry"
    assert b.dispatch_state is None and b.dispatch_started_at is None
    assert db.query(BookingNotification).count() == 0
    assert "needs a refund review" in caplog.text


def test_late_payment_exactly_at_the_lead_limit_is_refunded_not_reinstated(db):
    b = _expired_and_cancelled(db, timedelta(minutes=settings.LATE_PAYMENT_REINSTATE_MIN_LEAD_MINUTES))
    assert settle_booking_payment(db, b.id, 4_400_000, now=NOW) == SettleOutcome.PAID_NOT_DISPATCHED


def test_payment_for_a_booking_cancelled_for_another_reason_is_marked_for_refund(db):
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING,
                     status=BookingStatus.CANCELLED, total_amount_kobo=4_400_000)
    assert settle_booking_payment(db, b.id, 4_400_000, now=NOW) == SettleOutcome.PAID_NOT_DISPATCHED
    b = _fresh(db, b)
    assert b.refund_required is True and b.refund_required_reason == "paid_after_cancellation"


def test_expiry_racing_settlement_ends_consistent_either_way(db, monkeypatch):
    # whichever of the two wins the row lock, a paid booking with lead time ends up pending + dispatching
    import threading

    from app.db.session import SessionLocal

    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)
    real_now = datetime.utcnow()
    b = _unpaid(db, real_now - timedelta(minutes=1), total_amount_kobo=4_400_000)
    barrier, errors = threading.Barrier(2), []

    def run(fn):
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            fn(session)
        except Exception as exc:
            errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=run, args=(lambda s: expire_unpaid_bookings(s, now=real_now),)),
        threading.Thread(target=run, args=(lambda s: settle_booking_payment(s, b.id, 4_400_000, now=real_now),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    b = _fresh(db, b)
    assert b.payment_status == PaymentStatus.PAID
    assert b.status == BookingStatus.PENDING and b.cancel_reason is None
    assert b.dispatch_state == "dispatching" and b.refund_required is False


def test_create_booking_passes_the_frontend_callback_url_to_paystack(db, monkeypatch):
    db.add(BookingSettings(base_fee_kobo=500_000, platform_fee_percent=10.0))
    db.commit()
    seen = {}

    def fake_init(self, email, amount_kobo, reference, callback_url=None, metadata=None):
        seen["callback_url"] = callback_url
        return {"status": True, "authorization_url": "u", "access_code": "a", "reference": reference}

    monkeypatch.setattr(PaystackService, "initialize_transaction", fake_init)
    result = BookingService(db).create_booking(make_patient(db).id, BookingCreate(
        service_type="nurse", assistance_type="x", duration_hours=2, scheduled_date="2030-01-01T10:00:00",
        patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4,
        patient_city="Lagos", patient_state="Lagos", booking_for="myself"))
    assert seen["callback_url"] == f"{settings.FRONTEND_URL}/bookings/{result['booking'].id}/payment-complete"


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
