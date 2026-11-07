"""POST /booking/{id}/payment-link: "Pay now" for a booking that was created earlier, so it survives an app restart.

Money-adjacent, so the tests prove what must never happen: a second charge, a second transaction, a payment link for a
booking that is paid / cancelled / someone else's / past its payment window, a booking cancelled because Paystack hiccuped,
and a payment whose webhook was late being offered for payment again. Paystack is the shared fake.
"""

import threading
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.patient import PatientTransaction
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.services import booking_payment_link, dispatch
from app.services.paystack import PaystackService
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

API = "/api/v1/booking"
TOTAL = 4_400_000


@pytest.fixture(autouse=True)
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: calls.append(booking_id))
    return calls


@pytest.fixture
def patient(db):
    return make_patient(db)


def unpaid(db, patient, **kw):
    kw.setdefault("payment_expires_at", datetime.utcnow() + timedelta(minutes=20))
    kw.setdefault("payment_status", PaymentStatus.PENDING)
    return make_booking(db, patient, **kw)


def link(client, user, booking):
    return client.post(f"{API}/{booking.id}/payment-link", headers=auth_headers(user))


def reload(db, booking) -> Booking:
    db.expire_all()
    return db.get(Booking, booking.id)


def code(r) -> str:
    return r.json()["detail"]["code"]


def snapshot(b: Booking):
    return (b.status, b.payment_status, b.paystack_reference, b.payment_expires_at, b.cancel_reason,
            b.payment_failure_reason, b.refund_required, b.dispatch_state)


# --- the link ---------------------------------------------------------------------------------------------------------
def test_requires_a_patient(db, client, patient):
    b = unpaid(db, patient)
    assert client.post(f"{API}/{b.id}/payment-link").status_code == 401
    caregiver = make_caregiver(db)
    assert link(client, caregiver.user, b).status_code in (401, 403)


def test_an_unpaid_booking_gets_the_same_response_as_create(db, client, patient, fake_paystack):
    b = unpaid(db, patient)
    r = link(client, patient.user, b)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(b.id) and body["paystack_reference"] == b.paystack_reference
    assert body["payment_url"].startswith("https://checkout.paystack.com/")
    assert body["payment_access_code"] and body["payment_url"].endswith(body["payment_access_code"])
    assert body["status"] == "pending" and body["payment_status"] == "pending"
    assert body["payment_expires_at"] is not None
    assert body["total_amount"] == 44000.0 and body["payment_error"] is None
    sent = fake_paystack.initialised[0]
    assert (sent["reference"], sent["amount"]) == (b.paystack_reference, TOTAL)
    assert sent["callback_url"] == f"{settings.FRONTEND_URL}/bookings/{b.id}/payment-complete"
    assert sent["metadata"]["booking_id"] == str(b.id) and sent["metadata"]["booking_code"] == b.booking_code


def test_handing_out_a_link_writes_nothing(db, client, patient, fake_paystack):
    b = unpaid(db, patient)
    before = snapshot(reload(db, b))
    assert link(client, patient.user, b).status_code == 200
    assert snapshot(reload(db, b)) == before
    assert db.query(PatientTransaction).count() == 0 and db.query(BookingEvent).count() == 0
    assert db.query(Booking).count() == 1


def test_double_tap_is_the_same_reference_and_one_booking(db, client, patient, fake_paystack):
    b = unpaid(db, patient)
    first, second = link(client, patient.user, b), link(client, patient.user, b)
    assert first.status_code == second.status_code == 200
    assert first.json()["paystack_reference"] == second.json()["paystack_reference"] == b.paystack_reference
    assert {p["reference"] for p in fake_paystack.initialised} == {b.paystack_reference}  # never a second reference
    assert db.query(Booking).count() == 1 and db.query(PatientTransaction).count() == 0
    assert reload(db, b).payment_status == PaymentStatus.PENDING


def test_the_link_after_create_keeps_the_created_reference(db, client, patient, fake_paystack):
    """The app restarts after create: the booking comes back from the list and the link is asked for again. (The fake accepts
    a repeated reference; the real Paystack does not: the stored link covers that, see test_booking_payment_link_stored.py.)"""
    b = unpaid(db, patient)
    created = PaystackService().initialize_transaction("a@b.c", b.total_amount_kobo, b.paystack_reference)
    assert created["status"]
    again = link(client, patient.user, b).json()
    assert again["paystack_reference"] == b.paystack_reference
    assert {p["reference"] for p in fake_paystack.initialised} == {b.paystack_reference}


# --- who and what state -----------------------------------------------------------------------------------------------
def test_another_patient_is_refused_and_paystack_is_not_asked(db, client, patient, fake_paystack):
    b = unpaid(db, patient)
    other = make_patient(db)
    r = link(client, other.user, b)
    assert r.status_code == 403 and code(r) == "not_your_booking"
    assert fake_paystack.initialised == [] and fake_paystack.verified == []
    assert reload(db, b).payment_status == PaymentStatus.PENDING


def test_an_unknown_booking_is_a_404(client, patient, fake_paystack):
    import uuid

    r = client.post(f"{API}/{uuid.uuid4()}/payment-link", headers=auth_headers(patient.user))
    assert r.status_code == 404 and code(r) == "booking_not_found"


def test_a_paid_booking_is_refused_without_asking_paystack(db, client, patient, fake_paystack):
    b = make_booking(db, patient, payment_status=PaymentStatus.PAID)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_already_paid"
    assert r.json()["detail"]["payment_status"] == "paid" and r.json()["detail"]["refund_required"] is False
    assert fake_paystack.initialised == [] and fake_paystack.verified == []


@pytest.mark.parametrize("state", [PaymentStatus.REFUNDED, PaymentStatus.PARTIALLY_REFUNDED])
def test_a_refunded_booking_was_paid_too(db, client, patient, fake_paystack, state):
    b = make_booking(db, patient, status=BookingStatus.CANCELLED, payment_status=state)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_already_paid"


def test_a_cancelled_booking_is_refused(db, client, patient, fake_paystack):
    b = unpaid(db, patient, status=BookingStatus.CANCELLED, cancel_reason="user_cancelled")
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_already_cancelled"
    assert fake_paystack.initialised == [] and fake_paystack.verified == []
    assert reload(db, b).status == BookingStatus.CANCELLED


def test_a_booking_the_expiry_sweep_cancelled_is_refused(db, client, patient, fake_paystack):
    from app.services.booking_expiry import expire_unpaid_bookings

    b = unpaid(db, patient, payment_expires_at=datetime.utcnow() - timedelta(minutes=1))
    assert expire_unpaid_bookings(db) == 1
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_already_cancelled"


def test_a_failed_payment_booking_is_not_payable(db, client, patient, fake_paystack):
    b = unpaid(db, patient, payment_status=PaymentStatus.FAILED)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_not_payable"


def test_a_booking_with_no_reference_is_not_payable(db, client, patient, fake_paystack):
    b = unpaid(db, patient, paystack_reference=None)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_not_payable"
    assert fake_paystack.initialised == []


# --- the payment window -----------------------------------------------------------------------------------------------
def test_an_expired_window_is_refused_and_not_extended(db, client, patient, fake_paystack):
    closed = datetime.utcnow() - timedelta(minutes=1)
    b = unpaid(db, patient, payment_expires_at=closed)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "payment_window_expired"
    assert fake_paystack.initialised == []
    fresh = reload(db, b)
    assert fresh.payment_expires_at == closed and fresh.status == BookingStatus.PENDING  # the sweep cancels it, not us


def test_an_old_booking_without_an_expiry_uses_created_at_plus_the_ttl(db, client, patient, fake_paystack):
    b = unpaid(db, patient, payment_expires_at=None)
    b.created_at = datetime.utcnow() - timedelta(minutes=settings.PAYMENT_LINK_TTL_MINUTES + 5)
    db.commit()
    assert code(link(client, patient.user, b)) == "payment_window_expired"
    b.created_at = datetime.utcnow() - timedelta(minutes=5)
    db.commit()
    assert link(client, patient.user, b).status_code == 200


def test_the_last_moments_of_the_window_still_work(db, client, patient, fake_paystack):
    b = unpaid(db, patient, payment_expires_at=datetime.utcnow() + timedelta(seconds=30))
    assert link(client, patient.user, b).status_code == 200


# --- paid, but the webhook is late -------------------------------------------------------------------------------------
def _paid_at_paystack(db, fake_paystack, b):
    fake_paystack.initialize_transaction(None, "p@example.com", b.total_amount_kobo, b.paystack_reference)
    fake_paystack.paid.add(b.paystack_reference)


def test_paid_with_a_late_webhook_is_settled_not_offered_again(db, client, patient, fake_paystack, started):
    b = unpaid(db, patient)
    _paid_at_paystack(db, fake_paystack, b)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_already_paid"
    assert r.json()["detail"]["payment_status"] == "paid"
    assert len(fake_paystack.initialised) == 1                  # only the original charge: no second payment link
    assert reload(db, b).payment_status == PaymentStatus.PAID and len(started) == 1
    again = link(client, patient.user, b)                        # a repeat: already settled, nothing re-notified
    assert again.status_code == 409 and code(again) == "booking_already_paid" and len(started) == 1


def test_paid_in_time_but_settled_after_the_window_still_counts(db, client, patient, fake_paystack, started):
    b = unpaid(db, patient, payment_expires_at=datetime.utcnow() - timedelta(minutes=1))  # sweep has not run yet
    _paid_at_paystack(db, fake_paystack, b)
    r = link(client, patient.user, b)
    assert code(r) == "booking_already_paid" and len(fake_paystack.initialised) == 1
    assert reload(db, b).payment_status == PaymentStatus.PAID


def test_a_booking_the_sweep_already_cancelled_stays_with_the_webhook_and_refund_path(db, client, patient, fake_paystack):
    """Paid after the sweep cancelled it: that is the webhook's / verify-payment's case (paid_after_expiry refund), not a
    reason to hand out a link. The refusal is the cancelled one and nothing is initialised."""
    from app.services.booking_expiry import expire_unpaid_bookings

    b = unpaid(db, patient, payment_expires_at=datetime.utcnow() - timedelta(minutes=1))
    _paid_at_paystack(db, fake_paystack, b)
    assert expire_unpaid_bookings(db) == 1
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "booking_already_cancelled"
    assert len(fake_paystack.initialised) == 1


def test_a_paid_amount_that_does_not_match_is_held_for_review_not_marked_paid(db, client, patient, fake_paystack, started):
    b = unpaid(db, patient)
    fake_paystack.initialize_transaction(None, "p@example.com", 100_000, b.paystack_reference)  # Paystack charged 1,000
    fake_paystack.paid.add(b.paystack_reference)
    r = link(client, patient.user, b)
    assert r.status_code == 409 and code(r) == "payment_under_review"
    fresh = reload(db, b)
    assert fresh.payment_status == PaymentStatus.PENDING and "mismatch" in (fresh.payment_failure_reason or "").lower()
    assert started == [] and len(fake_paystack.initialised) == 1


# --- Paystack trouble ------------------------------------------------------------------------------------------------
def test_paystack_down_for_the_paid_check_issues_nothing(db, client, patient, fake_paystack, monkeypatch):
    b = unpaid(db, patient)
    monkeypatch.setattr(PaystackService, "verify_transaction",
                        lambda self, ref: {"status": False, "unreachable": True, "message": "Network error: timeout"})
    before = snapshot(reload(db, b))
    r = link(client, patient.user, b)
    assert r.status_code == 503 and code(r) == "payment_check_unavailable"
    assert fake_paystack.initialised == [] and snapshot(reload(db, b)) == before


def test_paystack_refusing_to_initialise_leaves_the_booking_untouched_and_payable(db, client, patient, fake_paystack):
    b = unpaid(db, patient)
    fake_paystack.fail_init = "Network error: connection reset"
    before = snapshot(reload(db, b))
    r = link(client, patient.user, b)
    assert r.status_code == 503 and code(r) == "payment_link_unavailable"
    assert "connection reset" not in r.text           # Paystack's wording is not shown to the patient
    assert snapshot(reload(db, b)) == before          # NOT cancelled, unlike /create: the person may hold a working link
    fake_paystack.fail_init = None
    assert link(client, patient.user, b).status_code == 200


def test_a_paystack_that_says_not_paid_for_an_unknown_reference_still_issues_the_link(db, client, patient, fake_paystack):
    b = unpaid(db, patient)  # never initialised at the fake: verify answers "reference not found", which is not "paid"
    assert link(client, patient.user, b).status_code == 200
    assert fake_paystack.verified == [b.paystack_reference]


def test_real_paystack_refusing_a_repeated_reference_for_a_booking_with_no_stored_link_is_a_503_and_untouched(db, client, patient, fake_paystack):
    """Paystack's error reference: a reference already used on the integration cannot be initialised again ("Duplicate
    Transaction Reference"). A booking /create initialised before migration 0023 has no stored link, so its link is a 503
    (its payment window closes on its own); never a charge, never a cancel. Bookings with a stored link: see
    test_booking_payment_link_stored.py."""
    b = unpaid(db, patient)
    fake_paystack.reject_duplicate_reference = True
    fake_paystack.initialize_transaction(None, "p@example.com", b.total_amount_kobo, b.paystack_reference)  # what /create did
    before = snapshot(reload(db, b))
    r = link(client, patient.user, b)
    assert r.status_code == 503 and code(r) == "payment_link_unavailable"
    assert "Duplicate" not in r.text and snapshot(reload(db, b)) == before


def test_a_whatsapp_booking_returns_to_the_whatsapp_page_and_only_its_booker_may_pay(db, client, patient, fake_paystack):
    other = make_patient(db)
    b = unpaid(db, patient, channel="whatsapp", booked_by_user_id=other.user_id)
    assert code(link(client, patient.user, b)) == "not_your_booking"      # the beneficiary is not the booker
    assert link(client, other.user, b).status_code == 200
    assert fake_paystack.initialised[-1]["callback_url"] == f"{settings.WHATSAPP_PAYMENT_RETURN_URL}?ref={b.booking_code}"


# --- races ------------------------------------------------------------------------------------------------------------
def _issue(user, booking):
    def job(session):
        return booking_payment_link.issue(session, session.merge(user, load=False), booking.id)
    return job


def race(*jobs):
    barrier, results = threading.Barrier(len(jobs)), {}

    def run(i, job):
        s = SessionLocal()
        try:
            barrier.wait()
            try:
                results[i] = job(s)
            except Exception as exc:  # noqa: BLE001 - the outcome is what the test inspects
                results[i] = exc
        finally:
            s.close()

    threads = [threading.Thread(target=run, args=(i, j)) for i, j in enumerate(jobs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return list(results.values())


def test_simultaneous_requests_never_make_a_second_reference_or_a_stray_row(db, patient, fake_paystack):
    b = unpaid(db, patient)
    out = race(*[_issue(patient.user, b) for _ in range(5)])
    won = [r for r in out if isinstance(r, dict)]
    lost = [r for r in out if not isinstance(r, dict)]
    assert won, out
    assert all(getattr(e, "code", None) == "payment_link_in_progress" for e in lost), lost
    assert {r["booking"].paystack_reference for r in won} == {b.paystack_reference}
    assert {p["reference"] for p in fake_paystack.initialised} == {b.paystack_reference}
    # winners run one at a time (the lock is released by the commit that stores the link): the first initialises the
    # reference and stores the link, every later winner is handed that stored link, every loser never asks Paystack
    assert len(fake_paystack.initialised) == 1
    assert len({r["payment_url"] for r in won}) == 1
    assert db.query(Booking).count() == 1 and db.query(PatientTransaction).count() == 0
    assert reload(db, b).payment_status == PaymentStatus.PENDING


def test_a_second_request_while_the_first_is_at_paystack_is_told_to_retry_then_the_lock_is_free(db, patient, fake_paystack, monkeypatch):
    b = unpaid(db, patient)
    inside, release, init_calls = threading.Event(), threading.Event(), []
    real = fake_paystack.initialize_transaction

    def slow_init(self, *a, **kw):
        init_calls.append(a)
        inside.set()
        assert release.wait(10)
        return real(self, *a, **kw)

    monkeypatch.setattr(PaystackService, "initialize_transaction", slow_init)
    first = {}

    def first_request():
        s = SessionLocal()
        try:
            first["result"] = booking_payment_link.issue(s, s.merge(patient.user, load=False), b.id)
        except Exception as exc:  # noqa: BLE001
            first["result"] = exc
        finally:
            s.close()

    t = threading.Thread(target=first_request)
    t.start()
    assert inside.wait(10)
    with pytest.raises(booking_payment_link.PatientCancelError) as busy:
        booking_payment_link.issue(db, patient.user, b.id)
    assert (busy.value.status_code, busy.value.code) == (409, "payment_link_in_progress")
    release.set()
    t.join()
    assert isinstance(first["result"], dict) and len(init_calls) == 1
    assert booking_payment_link.issue(db, patient.user, b.id)["payment_url"]  # lock released: the retry works


def test_a_payment_settling_while_a_link_is_requested_never_offers_a_second_payment(db, patient, fake_paystack, monkeypatch, started):
    """The webhook settles the booking while the first request is inside Paystack: the person is not charged twice, because
    the reference is the same and Paystack charges a reference once; and the booking ends paid exactly once."""
    from app.services.booking_payment import settle_booking_payment

    b = unpaid(db, patient)
    real = fake_paystack.initialize_transaction

    def init_then_webhook(self, *a, **kw):
        out = real(self, *a, **kw)
        s = SessionLocal()
        try:
            settle_booking_payment(s, b.id, b.total_amount_kobo)
        finally:
            s.close()
        return out

    monkeypatch.setattr(PaystackService, "initialize_transaction", init_then_webhook)
    booking_payment_link.issue(db, patient.user, b.id)
    assert reload(db, b).payment_status == PaymentStatus.PAID and len(started) == 1
    with pytest.raises(booking_payment_link.PatientCancelError) as paid:
        booking_payment_link.issue(db, patient.user, b.id)
    assert paid.value.code == "booking_already_paid" and len(started) == 1


# --- create then link, with pricing present -----------------------------------------------------------------
PREVIEW = {"service_type": "nurse", "duration_hours": 3}
CREATE = dict(service_type="nurse", assistance_type="x", duration_hours=3, scheduled_date="2030-01-01T10:00:00",
              patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
              patient_state="Lagos", booking_for="myself")


def test_with_pricing_preview_and_create_work_as_before(db, client, patient, fake_paystack):
    from app.tests.booking_fixtures import make_prices

    make_prices(db)
    assert client.post(f"{API}/preview", json=PREVIEW).status_code == 200
    r = client.post(f"{API}/create", json=CREATE, headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.json()["payment_url"]
    created = db.query(Booking).one()
    # ... and the payment link of that booking is the same transaction create started
    again = link(client, patient.user, created)
    assert again.status_code == 200 and again.json()["paystack_reference"] == created.paystack_reference
    assert {p["reference"] for p in fake_paystack.initialised} == {created.paystack_reference}  # create's, then the link's
