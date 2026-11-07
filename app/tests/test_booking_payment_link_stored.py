"""The Paystack link a booking was created with is kept (migration 0023) and handed back by POST /booking/{id}/payment-link.

Against the real Paystack a reference can be initialised once ("Duplicate Transaction Reference"), so the route must not ask
again for a booking that already has its link. Money-adjacent: the tests also prove the old guarantees still hold with a
stored link (a late-webhook payment is settled first, a Paystack failure never cancels, one reference per booking) and that
the stored link never rides along in another response. Paystack is the shared fake, set to reject duplicates like the real one.
"""

import json
from datetime import datetime, timedelta

import pytest

from app.api.v1.endpoints.booking import enrich_booking_with_details
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.services.booking import BookingService
from app.services import dispatch
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

API = "/api/v1/booking"
CREATE = dict(service_type="nurse", assistance_type="x", duration_hours=3, scheduled_date="2030-01-01T10:00:00",
              patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
              patient_state="Lagos", booking_for="myself")


@pytest.fixture(autouse=True)
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: calls.append(booking_id))
    return calls


@pytest.fixture
def patient(db):
    return make_patient(db)


@pytest.fixture
def pricing(db):
    db.add(BookingSettings(base_fee_kobo=123457, platform_fee_percent=7.5, multipliers={"nurse": 1.3}, discount=3.3))
    db.commit()


@pytest.fixture
def real_like(fake_paystack):
    fake_paystack.reject_duplicate_reference = True  # what the real Paystack does
    return fake_paystack


def reload(db, booking) -> Booking:
    db.expire_all()
    return db.get(Booking, booking.id)


def link(client, user, booking):
    return client.post(f"{API}/{booking.id}/payment-link", headers=auth_headers(user))


def create(client, patient):
    r = client.post(f"{API}/create", json=CREATE, headers=auth_headers(patient.user))
    assert r.status_code in (200, 201), r.text
    return r.json()


def code(r) -> str:
    return r.json()["detail"]["code"]


def snapshot(b: Booking):
    return (b.status, b.payment_status, b.paystack_reference, b.payment_expires_at, b.cancel_reason,
            b.payment_failure_reason, b.refund_required, b.dispatch_state, b.paystack_authorization_url,
            b.paystack_access_code)


# --- create keeps the link; the route hands it back --------------------------------------------------------------------
def test_create_stores_the_paystack_link_and_the_route_returns_the_same_one(db, client, patient, pricing, real_like):
    created = create(client, patient)
    b = db.query(Booking).one()
    assert b.paystack_authorization_url == created["payment_url"] and b.paystack_access_code == created["payment_access_code"]
    assert created["payment_url"].startswith("https://checkout.paystack.com/")

    again = link(client, patient.user, b)                     # the app restarted: "Pay now" asks for the link again
    assert again.status_code == 200, again.text
    assert again.json()["payment_url"] == created["payment_url"]
    assert again.json()["payment_access_code"] == created["payment_access_code"]
    assert again.json()["paystack_reference"] == b.paystack_reference
    assert len(real_like.initialised) == 1                     # Paystack was asked once, at create: never again
    assert len(real_like.verified) == 1                        # but the paid check still runs on every request


def test_a_repeat_and_a_double_tap_return_the_stored_link_and_write_nothing(db, client, patient, pricing, real_like):
    created = create(client, patient)
    b = db.query(Booking).one()
    before = snapshot(reload(db, b))
    first, second = link(client, patient.user, b), link(client, patient.user, b)
    assert first.status_code == second.status_code == 200
    assert first.json()["payment_url"] == second.json()["payment_url"] == created["payment_url"]
    assert snapshot(reload(db, b)) == before and len(real_like.initialised) == 1 and db.query(Booking).count() == 1


def test_whatsapp_start_payment_stores_the_link_too(db, patient, pricing, real_like):
    svc = BookingService(db)
    b = svc.insert_booking(patient.id, __import__("app.schemas.booking", fromlist=["BookingCreate"]).BookingCreate(**CREATE),
                           commit=True)
    out = svc.start_payment(b, callback_url="https://example.test/return")
    assert reload(db, b).paystack_authorization_url == out["payment_url"]
    assert reload(db, b).paystack_access_code == out["access_code"]


def test_a_failed_initialisation_at_create_stores_nothing_and_cancels_as_before(db, client, patient, pricing, fake_paystack):
    fake_paystack.fail_init = "Network error: connection reset"
    created = create(client, patient)
    b = db.query(Booking).one()
    assert created["payment_url"] is None and created["payment_error"]
    assert b.status == BookingStatus.CANCELLED and b.payment_status == PaymentStatus.FAILED
    assert b.paystack_authorization_url is None and b.paystack_access_code is None


def test_a_failure_to_save_the_link_never_loses_the_link_just_handed_out(db, patient, pricing, real_like, monkeypatch):
    from app.schemas.booking import BookingCreate

    svc = BookingService(db)
    b = svc.insert_booking(patient.id, BookingCreate(**CREATE), commit=True)
    real_commit = db.commit

    def broken():
        db.commit = real_commit
        raise RuntimeError("connection lost")

    monkeypatch.setattr(db, "commit", broken)
    out = svc.start_payment(b, callback_url="https://example.test/return")
    assert out["payment_url"].startswith("https://checkout.paystack.com/")      # the person still gets the link
    assert reload(db, b).paystack_authorization_url is None                      # and the booking is a "no stored link" one


# --- the old guarantees still hold with a stored link -------------------------------------------------------------------
def _paid_at_paystack(fake, b):
    fake.paid.add(b.paystack_reference)


def test_a_payment_with_a_late_webhook_is_settled_not_offered_again_even_with_a_stored_link(
        db, client, patient, pricing, real_like, started):
    create(client, patient)
    b = db.query(Booking).one()
    _paid_at_paystack(real_like, b)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_already_paid" and "payment_url" not in r.text
    fresh = reload(db, b)
    assert fresh.payment_status == PaymentStatus.PAID and len(started) == 1
    assert len(real_like.initialised) == 1


def test_a_paid_amount_that_does_not_match_is_held_for_review_with_a_stored_link(db, client, patient, pricing, real_like):
    create(client, patient)
    b = db.query(Booking).one()
    real_like.initialised[0]["amount"] = 100_000      # Paystack says it charged 1,000
    _paid_at_paystack(real_like, b)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "payment_under_review"
    assert reload(db, b).payment_status == PaymentStatus.PENDING


def test_paystack_down_for_the_paid_check_issues_no_stored_link(db, client, patient, pricing, real_like, monkeypatch):
    create(client, patient)
    b = db.query(Booking).one()
    monkeypatch.setattr(PaystackService, "verify_transaction",
                        lambda self, ref: {"status": False, "unreachable": True, "message": "Network error: timeout"})
    before = snapshot(reload(db, b))
    r = link(client, patient.user, b)
    assert r.status_code == 503 and code(r) == "payment_check_unavailable" and "checkout.paystack.com" not in r.text
    assert snapshot(reload(db, b)) == before


def test_an_expired_window_does_not_return_the_stored_link(db, client, patient, pricing, real_like):
    create(client, patient)
    b = db.query(Booking).one()
    b.payment_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "payment_window_expired" and "checkout.paystack.com" not in r.text


@pytest.mark.parametrize("state", [dict(payment_status=PaymentStatus.PAID),
                                   dict(status=BookingStatus.CANCELLED, cancel_reason="user_cancelled")])
def test_a_paid_or_cancelled_booking_never_gets_its_stored_link(db, client, patient, real_like, state):
    b = make_booking(db, patient, payment_expires_at=datetime.utcnow() + timedelta(minutes=20),
                     paystack_authorization_url="https://checkout.paystack.com/ac_old", paystack_access_code="ac_old", **state)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and "ac_old" not in r.text


def test_another_patient_cannot_read_the_stored_link(db, client, patient, pricing, real_like):
    create(client, patient)
    b = db.query(Booking).one()
    r = link(client, make_patient(db).user, b)
    assert r.status_code == 403 and code(r) == "not_your_booking" and "checkout.paystack.com" not in r.text


# --- bookings from before the migration (no stored link) ----------------------------------------------------------------
def _legacy(db, patient):
    return make_booking(db, patient, payment_status=PaymentStatus.PENDING,
                        payment_expires_at=datetime.utcnow() + timedelta(minutes=20))


def test_a_booking_with_no_stored_link_that_paystack_will_not_reinitialise_is_a_clear_503_and_untouched(
        db, client, patient, real_like):
    b = _legacy(db, patient)
    real_like.initialize_transaction(None, "p@example.com", b.total_amount_kobo, b.paystack_reference)  # what /create did
    before = snapshot(reload(db, b))
    r = link(client, patient.user, b)
    assert r.status_code == 503 and code(r) == "payment_link_unavailable"
    assert "Duplicate" not in r.text and snapshot(reload(db, b)) == before
    assert reload(db, b).status == BookingStatus.PENDING                   # never cancelled on a Paystack refusal


def test_a_booking_with_no_stored_link_that_paystack_initialises_keeps_that_link(db, client, patient, real_like):
    b = _legacy(db, patient)
    first = link(client, patient.user, b)
    assert first.status_code == 200, first.text
    assert reload(db, b).paystack_authorization_url == first.json()["payment_url"]
    second = link(client, patient.user, b)                                  # now served from the stored copy
    assert second.json()["payment_url"] == first.json()["payment_url"] and len(real_like.initialised) == 1


# --- the stored link rides in no other response -------------------------------------------------------------------------
def test_the_stored_link_is_in_no_booking_view(db, client, patient, pricing, real_like):
    created = create(client, patient)
    b = db.query(Booking).one()
    marker = created["payment_access_code"]
    detail = client.get(f"{API}/booking/{b.id}", headers=auth_headers(patient.user))
    listing = client.get(f"{API}/patient/bookings", headers=auth_headers(patient.user))
    assert detail.status_code == listing.status_code == 200
    for r in (detail, listing):
        assert marker not in r.text and "checkout.paystack.com" not in r.text, r.text
        assert "paystack_authorization_url" not in r.text and "paystack_access_code" not in r.text
    assert detail.json()["payment_url"] is None and detail.json()["payment_access_code"] is None
    enriched = enrich_booking_with_details(b, db)
    assert marker not in json.dumps(enriched, default=str)
    assert "paystack_authorization_url" not in enriched and "paystack_access_code" not in enriched


def test_a_caregiver_assigned_to_the_booking_never_sees_the_link(db, client, patient, real_like):
    caregiver = make_caregiver(db)
    b = make_booking(db, patient, caregiver=caregiver, status=BookingStatus.ACCEPTED,
                     payment_status=PaymentStatus.PAID,
                     paystack_authorization_url="https://checkout.paystack.com/ac_secret", paystack_access_code="ac_secret")
    for url in (f"{API}/booking/{b.id}", f"{API}/caregiver/bookings"):
        r = client.get(url, headers=auth_headers(caregiver.user))
        assert r.status_code == 200, r.text
        assert "ac_secret" not in r.text and "checkout.paystack.com" not in r.text


def test_a_failure_to_save_a_link_made_by_the_route_still_returns_it_and_leaves_the_booking_payable(
        db, client, patient, real_like, monkeypatch):
    b = _legacy(db, patient)
    real_commit = db.commit
    seen = {}

    def broken_once(*a, **kw):
        seen["raised"] = True
        raise RuntimeError("connection lost")

    from app.services import booking_payment_link as mod
    original = BookingService.store_payment_link

    def failing_store(self, booking, payment):
        self.db.commit = broken_once
        try:
            return original(self, booking, payment)
        finally:
            self.db.commit = real_commit

    monkeypatch.setattr(BookingService, "store_payment_link", failing_store)
    out = mod.issue(db, patient.user, b.id)
    assert seen["raised"] and out["payment_url"].startswith("https://checkout.paystack.com/")
    fresh = reload(db, b)
    assert fresh.status == BookingStatus.PENDING and fresh.payment_status == PaymentStatus.PENDING
    assert fresh.paystack_authorization_url is None            # not stored: the next request initialises again
    assert db.query(Booking).count() == 1                      # the session is still usable
