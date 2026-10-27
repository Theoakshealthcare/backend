"""Refunds v2 task 3: a pending refund row from every cancel path, with the policy's suggested amount; staff-raised refunds."""

from datetime import datetime, timedelta

import pytest

from app.db.models.audit import AdminAuditLog
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.refund import Refund
from app.services import booking_cancellation as cancellation
from app.services import refunds
from app.tests.factories import auth_headers, make_admin, make_booking, make_caregiver, make_patient
from app.tests.whatsapp_notify_helpers import Chat, accepted_booking, fake_meta, internal_dropout, onboarded  # noqa: F401

TOTAL = 4_400_000
SERVICE_FEE = 4_000_000
SNAPSHOT = {"free_hours": 12, "late_hours": 2, "fee_percent": 20, "late_fee_percent": 50}
ACTION = "/api/v1/booking/caregiver/action"


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def memory_booking(tier=None, **kw):
    return Booking(status=BookingStatus.CANCELLED, payment_status=PaymentStatus.PAID, total_amount_kobo=TOTAL,
                   service_fee_kobo=SERVICE_FEE, cancel_policy=SNAPSHOT, cancel_fee_tier=tier, **kw)


def refunds_of(db, booking):
    db.expire_all()
    return db.query(Refund).filter_by(booking_id=booking.id).all()


# --- suggestions (spec section 5) -------------------------------------------------------------------------------
def test_family_cancel_standard_tier_suggests_total_minus_20pct_of_service_fee():
    assert refunds.suggested_amount_kobo(memory_booking("standard"), "cancelled_by_booker", None) == 3_600_000


def test_family_cancel_late_tier_suggests_total_minus_50pct():
    assert refunds.suggested_amount_kobo(memory_booking("late"), "cancelled_by_booker", None) == 2_400_000


def test_the_suggestion_uses_the_frozen_policy_not_the_live_one():
    b = memory_booking("standard")
    b.cancel_policy = {**SNAPSHOT, "fee_percent": 10}
    assert refunds.suggested_amount_kobo(b, "cancelled_by_booker", None) == 4_000_000


@pytest.mark.parametrize("reason, entered", [
    ("cancelled_by_booker", None), ("caregiver_cancelled", None), ("no_caregiver_found", None),
    ("admin_cancelled", TOTAL), ("paid_after_expiry", None), ("paid_after_cancellation", None)])
def test_free_tier_and_no_fault_suggest_every_naira(reason, entered):
    assert refunds.suggested_amount_kobo(memory_booking("none"), reason, entered) == TOTAL


def test_paystack_fee_is_never_deducted():
    b = memory_booking("standard", paystack_fee_kobo=66_000)
    assert refunds.suggested_amount_kobo(b, "cancelled_by_booker", None) == 3_600_000
    assert refunds.suggested_amount_kobo(memory_booking("none", paystack_fee_kobo=66_000), "caregiver_cancelled", None) == TOTAL


def test_started_visit_dropout_has_no_suggestion():
    b = memory_booking(actual_start_time=datetime.utcnow())
    assert refunds.suggested_amount_kobo(b, "caregiver_cancelled", None) is None


def test_admin_partial_and_staff_reasons_suggest_what_was_entered():
    b = memory_booking()
    assert refunds.suggested_amount_kobo(b, "admin_cancelled", 1_000_000) == 1_000_000
    assert refunds.suggested_amount_kobo(b, "complaint", 700_000) == 700_000


# --- creation from the cancel paths -----------------------------------------------------------------------------
def test_whatsapp_cancel_by_booker_creates_pending_refund(db, chat):
    booking, _ = accepted_booking(db, chat, hours_ahead=6)
    assert cancellation.cancel_by_booker(db, booking.id).status == "cancelled"
    [refund] = refunds_of(db, booking)
    assert (refund.origin, refund.status, refund.reason_code) == ("system", "pending_approval", "cancelled_by_booker")
    assert refund.suggested_amount_kobo == TOTAL - 800_000 and refund.amount_kobo is None
    assert refund.patient_id == booking.patient_id and refund.requested_by_admin_id is None
    # The flag stays set on purpose: the family's "refund being processed" notice is dropped as stale without it.
    assert db.get(Booking, booking.id).refund_required is True


def test_flag_refund_twice_creates_one_refund(db):
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED)
    cancellation.flag_refund(booking, "caregiver_cancelled", TOTAL)
    cancellation.flag_refund(booking, "caregiver_cancelled", TOTAL)
    db.commit()
    assert len(refunds_of(db, booking)) == 1


def test_admin_reason_codes_map_onto_admin_cancelled(db):
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED)
    cancellation.flag_refund(booking, "admin_cancelled_partial", 1_000_000)
    db.commit()
    [refund] = refunds_of(db, booking)
    assert refund.reason_code == "admin_cancelled" and refund.suggested_amount_kobo == 1_000_000


def test_unpaid_booking_creates_no_refund(db):
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED, payment_status=PaymentStatus.PENDING)
    cancellation.flag_refund(booking, "cancelled_by_booker", TOTAL)
    db.commit()
    assert refunds_of(db, booking) == []


def test_app_caregiver_cancel_of_paid_booking_queues_full_refund(db, client):
    cg = make_caregiver(db)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, payment_status=PaymentStatus.PAID,
                           channel="app")
    r = client.post(f"{ACTION}/{booking.id}", json={"action": "cancel"}, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    [refund] = refunds_of(db, booking)
    assert refund.reason_code == "caregiver_cancelled" and refund.suggested_amount_kobo == TOTAL
    assert refund.status == "pending_approval" and refund.origin == "system"


def test_app_caregiver_cancel_of_unpaid_booking_creates_no_refund(db, client):
    cg = make_caregiver(db)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.ACCEPTED, payment_status=PaymentStatus.PENDING,
                           channel="app")
    r = client.post(f"{ACTION}/{booking.id}", json={"action": "cancel"}, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    assert refunds_of(db, booking) == []


def test_whatsapp_caregiver_dropout_still_redispatches(db, chat):
    booking, cg = accepted_booking(db, chat, hours_ahead=48)
    internal_dropout(db, booking, cg)
    db.expire_all()
    b = db.get(Booking, booking.id)
    assert b.status == BookingStatus.PENDING and b.caregiver_id is None
    assert refunds_of(db, booking) == []


def test_payment_after_cancellation_queues_a_refund(db):
    from app.services.booking_payment import settle_booking_payment
    booking = make_booking(db, make_patient(db), status=BookingStatus.CANCELLED, payment_status=PaymentStatus.PENDING,
                           cancel_reason="unpaid_timeout", scheduled_date=datetime.utcnow() + timedelta(minutes=5))
    settle_booking_payment(db, booking.id, TOTAL)
    [refund] = refunds_of(db, booking)
    assert refund.reason_code == "paid_after_expiry" and refund.suggested_amount_kobo == TOTAL


# --- staff-raised ------------------------------------------------------------------------------------------------
def test_staff_request_creates_a_staff_refund_and_audits_it(db):
    admin = make_admin(db)
    booking = make_booking(db, make_patient(db), status=BookingStatus.COMPLETED)
    refund = refunds.request_refund(db, booking_id=booking.id, amount_kobo=500_000, reason_code="complaint",
                                    note="late arrival", admin=admin)
    assert (refund.origin, refund.status, refund.requested_by_admin_id) == ("staff", "pending_approval", admin.id)
    assert refund.suggested_amount_kobo == 500_000 and refund.note == "late arrival"
    assert db.query(AdminAuditLog).filter_by(action="refund.request", entity_id=str(refund.id)).count() == 1


def test_staff_request_rejects_more_than_refundable(db):
    booking = make_booking(db, make_patient(db), status=BookingStatus.COMPLETED)
    with pytest.raises(refunds.RefundInvalid):
        refunds.request_refund(db, booking_id=booking.id, amount_kobo=TOTAL + 1, reason_code="other", note=None,
                               admin=make_admin(db))
    assert refunds_of(db, booking) == []


def test_staff_request_rejects_an_unpaid_booking(db):
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    with pytest.raises(refunds.RefundInvalid):
        refunds.request_refund(db, booking_id=booking.id, amount_kobo=1, reason_code="other", note=None, admin=make_admin(db))


def test_staff_request_conflicts_with_open_refund(db):
    admin = make_admin(db)
    booking = make_booking(db, make_patient(db), status=BookingStatus.COMPLETED)
    refunds.request_refund(db, booking_id=booking.id, amount_kobo=100, reason_code="other", note=None, admin=admin)
    with pytest.raises(refunds.RefundConflict):
        refunds.request_refund(db, booking_id=booking.id, amount_kobo=100, reason_code="other", note=None, admin=admin)
    assert len(refunds_of(db, booking)) == 1


def test_staff_request_unknown_booking_is_not_found(db):
    import uuid
    with pytest.raises(refunds.RefundNotFound):
        refunds.request_refund(db, booking_id=uuid.uuid4(), amount_kobo=1, reason_code="other", note=None, admin=make_admin(db))
