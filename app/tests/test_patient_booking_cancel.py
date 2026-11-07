"""P4-BE: the patient app cancels a booking (``GET .../cancel-terms``, ``POST .../cancel``) and sees the refund.

The money rules themselves are covered by test_cancellation_tiers (the policy) and the refunds tests (the queue). These tests
prove the app channel: who may, the stable error codes, idempotency, the unpaid path, never a second fee or refund, a refund
that is never automatic and never to the wallet, and the races with a caregiver accepting or starting.
"""

import threading
from datetime import datetime, timedelta

import pytest

from app.core.security import decrypt
from app.db.models.booking import Booking, BookingNotification, BookingStatus, PaymentStatus
from app.db.models.patient import PatientTransaction
from app.db.models.refund import Refund, RefundAttempt
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.schemas.booking import BookingActionRequest
from app.services import booking_cancellation as cancellation, patient_booking_cancel as cancel_service
from app.services.booking import BookingService
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient, make_user
from app.tests.policy_helpers import set_policy

API = "/api/v1/booking"
TOTAL = 4_400_000
SERVICE_FEE = 4_000_000          # make_booking: total / 1.1
STANDARD_FEE = 800_000           # 20% of the service fee
LATE_FEE = 2_000_000             # 50% of the service fee
NOW = datetime(2026, 11, 7, 9, 0, 0)


@pytest.fixture(autouse=True)
def _policy(db):
    set_policy(db)  # the live policy (defaults 12 h / 2 h / 20% / 50%): app bookings carry no snapshot


def hours(h, now=None):
    return (now or datetime.utcnow()) + timedelta(hours=h)


def booking_in(db, patient, h, status=BookingStatus.ACCEPTED, caregiver=True, **kw):
    cg = make_caregiver(db) if caregiver and status != BookingStatus.PENDING else None
    return make_booking(db, patient, cg, status=status, scheduled_date=hours(h), scheduled_end_date=hours(h + 4),
                        visit_code="4821" if cg else None, **kw)


def terms(client, user, booking):
    return client.get(f"{API}/{booking.id}/cancel-terms", headers=auth_headers(user))


def cancel(client, user, booking, **body):
    return client.post(f"{API}/{booking.id}/cancel", json=body or None, headers=auth_headers(user))


def reload(db, booking) -> Booking:
    db.expire_all()
    return db.get(Booking, booking.id)


def refunds(db, booking):
    db.expire_all()
    return db.query(Refund).filter(Refund.booking_id == booking.id).all()


def code(response) -> str:
    return response.json()["detail"]["code"]


@pytest.fixture
def patient(db):
    return make_patient(db)


# --- what it would cost -------------------------------------------------------------------------------------------
def test_terms_for_a_booking_still_looking_for_a_caregiver_are_free_with_a_full_refund(db, client, patient):
    booking = booking_in(db, patient, 1, BookingStatus.PENDING)   # an hour away, but nobody assigned: free
    body = terms(client, patient.user, booking).json()
    assert terms(client, patient.user, booking).status_code == 200
    assert body["can_cancel"] and body["reason"] is None
    assert (body["fee_tier"], body["fee_amount_kobo"], body["refundable_amount_kobo"]) == ("none", 0, TOTAL)
    assert (body["fee_amount"], body["paid_amount"], body["refundable_amount"]) == (0.0, 44000.0, 44000.0)
    assert body["refund_will_be_queued"] is True and body["refund_needs_approval"] is True
    assert body["refund_destination"] == "original_method" and body["caregiver_assigned"] is False
    assert body["policy"] == {"free_hours": 12, "late_hours": 2, "fee_percent": 20, "late_fee_percent": 50}


@pytest.mark.parametrize("h, status, tier, fee", [
    (30, BookingStatus.ACCEPTED, "none", 0),
    (5, BookingStatus.ACCEPTED, "standard", STANDARD_FEE),
    (1, BookingStatus.ACCEPTED, "late", LATE_FEE),
])
def test_terms_follow_the_tiers(db, client, patient, h, status, tier, fee):
    booking = booking_in(db, patient, h, status)
    body = terms(client, patient.user, booking).json()
    assert (body["fee_tier"], body["fee_amount_kobo"], body["refundable_amount_kobo"]) == (tier, fee, TOTAL - fee)
    assert body["fee_percent"] == {"none": 0, "standard": 20, "late": 50}[tier]
    assert body["caregiver_assigned"] is True and body["refund_will_be_queued"] is True


def test_a_caregiver_on_the_way_in_good_time_is_the_late_tier_even_far_out(db, client, patient):
    booking = booking_in(db, patient, 1.5, BookingStatus.EN_ROUTE, on_my_way_at=hours(-0.1))
    body = terms(client, patient.user, booking).json()
    assert (body["fee_tier"], body["fee_amount_kobo"]) == ("late", LATE_FEE)


@pytest.mark.parametrize("delta_s, tier, fee", [
    (12 * 3600 + 1, "none", 0),                 # one second over 12 h: free
    (12 * 3600, "standard", STANDARD_FEE),      # exactly 12 h: the fee starts
    (2 * 3600 + 1, "standard", STANDARD_FEE),
    (2 * 3600, "standard", STANDARD_FEE),       # exactly 2 h is not "under 2 h"
    (2 * 3600 - 1, "late", LATE_FEE),           # one second under: the late fee
])
def test_the_fee_boundaries_at_exactly_12_h_and_2_h(db, patient, delta_s, tier, fee):
    booking = make_booking(db, patient, make_caregiver(db), status=BookingStatus.ACCEPTED,
                           scheduled_date=NOW + timedelta(seconds=delta_s))
    view = cancel_service.terms_view(db, patient.user, booking.id, now=NOW)
    assert (view.fee_tier, view.fee_kobo, view.refundable_kobo) == (tier, fee, TOTAL - fee)
    result = cancel_service.cancel(db, patient.user, booking.id, accepted_fee_kobo=fee, now=NOW)
    assert (reload(db, booking).cancel_fee_tier, result.view.fee_kobo) == (tier, fee)
    assert refunds(db, booking)[0].suggested_amount_kobo == TOTAL - fee


@pytest.mark.parametrize("make, expect", [
    (lambda db, p: booking_in(db, p, 5, BookingStatus.IN_PROGRESS), "booking_in_progress"),
    (lambda db, p: booking_in(db, p, -5, BookingStatus.COMPLETED), "booking_completed"),
    (lambda db, p: booking_in(db, p, 5, BookingStatus.CANCELLED), "booking_already_cancelled"),
    (lambda db, p: booking_in(db, p, 5, BookingStatus.DECLINED), "booking_closed"),
])
def test_terms_say_why_it_cannot_be_cancelled(db, client, patient, make, expect):
    body = terms(client, patient.user, make(db, patient)).json()
    assert body["can_cancel"] is False and body["reason"] == expect and body["message"]
    assert (body["fee_amount_kobo"], body["refundable_amount_kobo"], body["refund_will_be_queued"]) == (0, 0, False)


def test_terms_for_an_unpaid_booking_are_free_and_queue_no_refund(db, client, patient):
    booking = booking_in(db, patient, 5, BookingStatus.PENDING, payment_status=PaymentStatus.PENDING)
    body = terms(client, patient.user, booking).json()
    assert body["can_cancel"] and (body["fee_tier"], body["fee_amount_kobo"]) == ("none", 0)
    assert body["refund_will_be_queued"] is False and body["refund_destination"] is None and body["paid_amount_kobo"] == 0


# --- who may -----------------------------------------------------------------------------------------------------
def test_error_codes_are_distinct_and_stable(db, client, patient):
    booking = booking_in(db, patient, 5)
    stranger = make_patient(db)
    caregiver_user = make_user(db, "caregiver")
    missing = "00000000-0000-0000-0000-000000000000"
    for verb, url in (("get", f"{API}/{booking.id}/cancel-terms"), ("post", f"{API}/{booking.id}/cancel")):
        other = getattr(client, verb)(url, headers=auth_headers(stranger.user))
        assert (other.status_code, code(other)) == (403, "not_your_booking")
        none = getattr(client, verb)(url.replace(str(booking.id), missing), headers=auth_headers(patient.user))
        assert (none.status_code, code(none)) == (404, "booking_not_found")
        assert getattr(client, verb)(url).status_code == 401
        assert getattr(client, verb)(url, headers=auth_headers(caregiver_user)).status_code == 403  # not a patient account
    assert reload(db, booking).status == BookingStatus.ACCEPTED and not refunds(db, booking)


def test_the_patient_of_a_booking_somebody_else_made_is_not_its_booker(db, client, patient):
    booker = make_user(db, "patient")
    booking = booking_in(db, patient, 5, booked_by_user_id=booker.id, channel="whatsapp")
    assert terms(client, patient.user, booking).status_code == 403
    assert cancel(client, patient.user, booking).status_code == 403
    assert cancel(client, booker, booking).status_code == 200  # the booker (a WhatsApp booking's maker) may, as in chat


@pytest.mark.parametrize("make, expect", [
    (lambda db, p: booking_in(db, p, 5, BookingStatus.IN_PROGRESS), "booking_in_progress"),
    (lambda db, p: booking_in(db, p, -5, BookingStatus.COMPLETED), "booking_completed"),
    (lambda db, p: booking_in(db, p, 5, BookingStatus.DECLINED), "booking_closed"),
    (lambda db, p: booking_in(db, p, 5, BookingStatus.CANCELLED, cancel_reason="admin_cancelled"), "booking_already_cancelled"),
    (lambda db, p: booking_in(db, p, 5, BookingStatus.CANCELLED, cancel_reason="unpaid_timeout"), "booking_already_cancelled"),
])
def test_a_booking_in_the_wrong_state_is_a_409_with_a_code_and_nothing_changes(db, client, patient, make, expect):
    booking = make(db, patient)
    before = reload(db, booking).status
    response = cancel(client, patient.user, booking)
    assert (response.status_code, code(response)) == (409, expect) and response.json()["detail"]["message"]
    assert reload(db, booking).status == before and not refunds(db, booking)


# --- the cancel ---------------------------------------------------------------------------------------------------
def test_cancelling_an_accepted_booking_queues_a_refund_for_a_person_and_moves_no_money(db, client, patient):
    booking = booking_in(db, patient, 5)
    caregiver_id = booking.caregiver_id
    body = cancel(client, patient.user, booking, reason="Plans changed", accepted_fee_kobo=STANDARD_FEE).json()
    assert body["status"] == "cancelled" and body["already_cancelled"] is False
    assert (body["fee_tier"], body["fee_amount_kobo"], body["refundable_amount_kobo"]) == ("standard", STANDARD_FEE, TOTAL - STANDARD_FEE)
    assert body["refund_queued"] and body["refund_destination"] == "original_method" and body["refund_needs_approval"]
    assert body["refund"] == {"status": "under_review", "amount": 36000.0}
    row = reload(db, booking)
    assert (row.status, row.cancel_fee_tier, row.refund_required, row.refund_amount_kobo) == (
        BookingStatus.CANCELLED, "standard", True, TOTAL - STANDARD_FEE)
    [refund] = refunds(db, booking)
    assert (refund.status, refund.origin, refund.reason_code) == ("pending_approval", "system", "cancelled_by_booker")
    assert (refund.suggested_amount_kobo, refund.amount_kobo, refund.destination) == (TOTAL - STANDARD_FEE, None, None)
    assert "Plans changed" in refund.note
    assert db.query(RefundAttempt).count() == 0 and db.query(PatientTransaction).count() == 0  # nothing sent, nothing credited
    event = db.query(BookingEvent).filter_by(booking_id=booking.id, event="caregiver_booking_cancelled").one()
    assert event.dedupe_key == str(caregiver_id)  # the assigned caregiver is told, as for a WhatsApp cancel


def test_the_cancel_works_without_a_body(db, client, patient):
    booking = booking_in(db, patient, 30)
    response = client.post(f"{API}/{booking.id}/cancel", headers=auth_headers(patient.user))
    assert response.status_code == 200 and response.json()["fee_tier"] == "none"
    assert refunds(db, booking)[0].suggested_amount_kobo == TOTAL and refunds(db, booking)[0].note is None


def test_the_wallet_is_never_offered(db, client, patient):
    booking = booking_in(db, patient, 5)
    seen = terms(client, patient.user, booking).text + cancel(client, patient.user, booking).text
    assert "wallet" not in seen.lower()
    assert refunds(db, booking)[0].destination is None  # chosen by the approver; the family is only ever told original_method


def test_cancelling_twice_is_one_fee_and_one_refund_and_replays_the_first_answer(db, client, patient):
    booking = booking_in(db, patient, 5)
    first = cancel(client, patient.user, booking, accepted_fee_kobo=STANDARD_FEE)
    again = cancel(client, patient.user, booking)
    third = cancel(client, patient.user, booking, accepted_fee_kobo=0)  # a retry with other numbers still replays
    assert (first.status_code, again.status_code, third.status_code) == (200, 200, 200)
    assert again.json()["already_cancelled"] is True and first.json()["already_cancelled"] is False
    keep = ("fee_tier", "fee_amount_kobo", "refundable_amount_kobo", "refund")
    assert {k: again.json()[k] for k in keep} == {k: first.json()[k] for k in keep} == {k: third.json()[k] for k in keep}
    assert len(refunds(db, booking)) == 1 and reload(db, booking).refund_amount_kobo == TOTAL - STANDARD_FEE
    assert db.query(BookingEvent).filter_by(booking_id=booking.id, event="caregiver_booking_cancelled").count() == 1


def test_a_changed_fee_cancels_nothing_and_returns_the_new_terms(db, client, patient):
    booking = booking_in(db, patient, 5)   # the family saw "free" (fee 0), but the booking is 20% now
    response = cancel(client, patient.user, booking, accepted_fee_kobo=0)
    assert (response.status_code, code(response)) == (409, "terms_changed")
    new = response.json()["detail"]["terms"]
    assert (new["fee_tier"], new["fee_amount_kobo"], new["can_cancel"]) == ("standard", STANDARD_FEE, True)
    assert reload(db, booking).status == BookingStatus.ACCEPTED and not refunds(db, booking)


def test_a_lower_fee_than_the_one_shown_is_fine(db, client, patient):
    booking = booking_in(db, patient, 30)
    assert cancel(client, patient.user, booking, accepted_fee_kobo=STANDARD_FEE).status_code == 200
    assert reload(db, booking).cancel_fee_tier == "none"


def test_a_malformed_body_is_a_422_and_cancels_nothing(db, client, patient):
    booking = booking_in(db, patient, 5)
    assert cancel(client, patient.user, booking, accepted_fee_kobo=-1).status_code == 422
    assert cancel(client, patient.user, booking, reason="x" * 501).status_code == 422
    assert reload(db, booking).status == BookingStatus.ACCEPTED


@pytest.mark.parametrize("h, status, kw", [
    (30, BookingStatus.ACCEPTED, {}), (5, BookingStatus.ACCEPTED, {}), (1, BookingStatus.ACCEPTED, {}),
    (1, BookingStatus.PENDING, {}),
])
def test_the_app_and_whatsapp_cancel_store_the_same_thing(db, client, patient, h, status, kw):
    """Same booking, once through the app endpoint, once through the function WhatsApp calls: identical outcome."""
    via_app, via_chat = booking_in(db, patient, h, status, **kw), booking_in(db, make_patient(db), h, status, **kw)
    assert cancel(client, patient.user, via_app).status_code == 200
    assert cancellation.cancel_by_booker(db, via_chat.id).status == "cancelled"
    a, b = reload(db, via_app), reload(db, via_chat)
    assert (a.cancel_fee_tier, a.refund_amount_kobo, a.cancel_reason, a.refund_required_reason) == (
        b.cancel_fee_tier, b.refund_amount_kobo, b.cancel_reason, b.refund_required_reason)
    assert refunds(db, via_app)[0].suggested_amount_kobo == refunds(db, via_chat)[0].suggested_amount_kobo


# --- an unpaid booking ---------------------------------------------------------------------------------------------
def unpaid(db, patient, fake_paystack, **kw):
    booking = booking_in(db, patient, 5, BookingStatus.PENDING, payment_status=PaymentStatus.PENDING, **kw)
    fake_paystack.initialised.append({"reference": booking.paystack_reference, "amount": booking.total_amount_kobo,
                                      "email": patient.email})
    return booking


def test_cancelling_before_paying_is_free_and_creates_no_refund(db, client, patient, fake_paystack):
    booking = unpaid(db, patient, fake_paystack)
    response = cancel(client, patient.user, booking)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "cancelled"
    assert (body["fee_tier"], body["fee_amount_kobo"], body["paid_amount_kobo"], body["refundable_amount_kobo"]) == ("none", 0, 0, 0)
    assert body["refund_queued"] is False and body["refund"] is None and body["refund_destination"] is None
    row = reload(db, booking)
    assert (row.status, row.cancel_reason, row.refund_required) == (BookingStatus.CANCELLED, "user_cancelled", False)
    assert not refunds(db, booking) and booking.paystack_reference in fake_paystack.verified  # Paystack was asked first
    again = cancel(client, patient.user, booking)  # a retry: the same answer, no second anything
    assert (again.status_code, again.json()["already_cancelled"], again.json()["refund"]) == (200, True, None)


def test_a_payment_the_webhook_has_not_delivered_yet_is_not_cancelled_away(db, client, patient, fake_paystack):
    """They paid on the link a second ago: the app still shows 'unpaid'. The cancel settles the payment first, then cancels the
    now paid booking, so the money comes back by the refund queue instead of vanishing."""
    booking = unpaid(db, patient, fake_paystack)
    fake_paystack.paid.add(booking.paystack_reference)
    body = cancel(client, patient.user, booking, accepted_fee_kobo=0).json()
    assert (body["fee_tier"], body["paid_amount_kobo"], body["refundable_amount_kobo"]) == ("none", TOTAL, TOTAL)
    assert body["refund"] == {"status": "under_review", "amount": 44000.0}
    row = reload(db, booking)
    assert (row.status, row.payment_status) == (BookingStatus.CANCELLED, PaymentStatus.PAID)
    assert [r.suggested_amount_kobo for r in refunds(db, booking)] == [TOTAL]


def test_when_paystack_cannot_be_asked_nothing_is_cancelled(db, client, patient, fake_paystack, monkeypatch):
    from app.services.paystack import PaystackService
    booking = unpaid(db, patient, fake_paystack)
    monkeypatch.setattr(PaystackService, "verify_transaction", lambda self, ref: {"status": False, "unreachable": True})
    response = cancel(client, patient.user, booking)
    assert (response.status_code, code(response)) == (503, "payment_check_unavailable")
    assert reload(db, booking).status == BookingStatus.PENDING and not refunds(db, booking)


def test_a_payment_with_the_wrong_amount_goes_to_a_person(db, client, patient, fake_paystack):
    booking = unpaid(db, patient, fake_paystack)
    fake_paystack.initialised[-1]["amount"] = TOTAL - 1  # Paystack took a different amount than the booking total
    fake_paystack.paid.add(booking.paystack_reference)
    response = cancel(client, patient.user, booking)
    assert (response.status_code, code(response)) == (409, "payment_under_review")
    assert reload(db, booking).status == BookingStatus.PENDING and not refunds(db, booking)


# --- never more than was paid ---------------------------------------------------------------------------------------
def test_the_refund_is_never_more_than_what_is_left_of_what_was_paid(db, client, patient):
    booking = booking_in(db, patient, 30)
    other = Refund(booking_id=booking.id, patient_id=patient.id, reason_code="complaint", origin="staff",
                   suggested_amount_kobo=4_000_000, amount_kobo=4_000_000, destination="original_method", status="succeeded")
    db.add(other)
    db.commit()  # 40,000 of the 44,000 is already refunded (a closed-out earlier refund)
    body = cancel(client, patient.user, booking).json()
    assert body["refundable_amount_kobo"] == 400_000 and body["refund"]["amount"] == 4000.0
    [_, fresh] = sorted(refunds(db, booking), key=lambda r: r.created_at)
    assert fresh.suggested_amount_kobo == 400_000
    assert sum(r.suggested_amount_kobo for r in refunds(db, booking)) <= TOTAL


def test_a_booking_that_already_has_an_open_refund_gets_no_second_one(db, client, patient):
    booking = booking_in(db, patient, 5)
    db.add(Refund(booking_id=booking.id, patient_id=patient.id, reason_code="complaint", origin="staff",
                  suggested_amount_kobo=100_000, status="pending_approval"))
    db.commit()
    assert cancel(client, patient.user, booking).status_code == 200
    assert len(refunds(db, booking)) == 1


# --- what the app reads back ----------------------------------------------------------------------------------------
def test_the_booking_responses_carry_the_fee_and_the_refund_to_the_patient_only(db, client, patient):
    booking = booking_in(db, patient, 5)
    detail = lambda user: client.get(f"{API}/booking/{booking.id}", headers=auth_headers(user)).json()  # noqa: E731
    mine = detail(patient.user)
    assert mine["visit_code"] == "4821" and (mine["cancel_fee_tier"], mine["cancel_fee"], mine["refund"]) == (None, None, None)
    cancel(client, patient.user, booking)
    mine = detail(patient.user)
    assert (mine["cancel_fee_tier"], mine["cancel_fee"], mine["refund"]) == ("standard", 8000.0, {"status": "under_review", "amount": 36000.0})
    assert mine["visit_code"] is None  # cancelled: the code is no longer shown
    [listed] = client.get(f"{API}/patient/bookings", headers=auth_headers(patient.user)).json()
    assert (listed["cancel_fee_tier"], listed["cancel_fee"], listed["refund"]) == ("standard", 8000.0, mine["refund"])
    assert listed["visit_code"] is None
    caregiver_view = client.get(f"{API}/booking/{booking.id}", headers=auth_headers(booking_caregiver_user(db, booking))).json()
    assert (caregiver_view["cancel_fee_tier"], caregiver_view["cancel_fee"], caregiver_view["refund"]) == (None, None, None)


def booking_caregiver_user(db, booking):
    from app.db.models.caregiver import Caregiver
    return db.get(Caregiver, booking.caregiver_id).user


def test_the_visit_code_is_for_the_patient_while_accepted_or_en_route_only(db, client, patient):
    accepted, en_route = booking_in(db, patient, 5), booking_in(db, patient, 1, BookingStatus.EN_ROUTE)
    pending = booking_in(db, patient, 5, BookingStatus.PENDING)
    seen = {b.id: client.get(f"{API}/booking/{b.id}", headers=auth_headers(patient.user)).json()["visit_code"]
            for b in (accepted, en_route, pending)}
    assert seen == {accepted.id: "4821", en_route.id: "4821", pending.id: None}
    assert all(b["visit_code"] is None for b in client.get(f"{API}/patient/bookings", headers=auth_headers(patient.user)).json())


@pytest.mark.parametrize("status, expect", [
    ("pending_approval", "under_review"), ("needs_review", "under_review"), ("approved", "processing"),
    ("processing", "processing"), ("failed", "processing"), ("needs_attention", "needs_bank_details"),
    ("succeeded", "refunded"), ("rejected", "declined"), ("closed", "contact_support"),
])
def test_the_family_sees_a_simple_refund_status(db, patient, status, expect):
    booking = booking_in(db, patient, 5, BookingStatus.CANCELLED, cancel_reason="cancelled_by_booker")
    db.add(Refund(booking_id=booking.id, patient_id=patient.id, reason_code="cancelled_by_booker", origin="system",
                  suggested_amount_kobo=3_600_000, amount_kobo=3_000_000 if status != "pending_approval" else None,
                  status=status))
    db.commit()
    assert cancel_service.refund_summaries(db, [booking.id])[booking.id] == {
        "status": expect, "amount": 30000.0 if status != "pending_approval" else 36000.0}


# --- races ---------------------------------------------------------------------------------------------------------
def race(*jobs):
    barrier, results = threading.Barrier(len(jobs)), {}

    def run(name, job):
        s = SessionLocal()
        try:
            barrier.wait()
            try:
                results[name] = job(s)
            except Exception as exc:  # noqa: BLE001 - the outcome is what the test inspects
                results[name] = exc
        finally:
            s.close()

    threads = [threading.Thread(target=run, args=job) for job in jobs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def do_cancel(user, booking, **kw):
    return lambda s: cancel_service.cancel(s, s.merge(user, load=False), booking.id, **kw)


def test_two_simultaneous_cancels_are_one_fee_and_one_refund(db, patient):
    booking = booking_in(db, patient, 5)
    out = race(("a", do_cancel(patient.user, booking)), ("b", do_cancel(patient.user, booking)),
               ("c", do_cancel(patient.user, booking)))
    assert all(isinstance(r, cancel_service.Result) for r in out.values()), out
    assert sorted(r.already_cancelled for r in out.values()) == [False, True, True]
    assert {(r.view.fee_tier, r.view.fee_kobo, r.view.refundable_kobo) for r in out.values()} == {("standard", STANDARD_FEE, TOTAL - STANDARD_FEE)}
    assert [r.suggested_amount_kobo for r in refunds(db, booking)] == [TOTAL - STANDARD_FEE]
    assert db.query(BookingEvent).filter_by(booking_id=booking.id, event="caregiver_booking_cancelled").count() == 1


def test_cancel_racing_a_caregiver_accept_never_leaves_a_refunded_booking_assigned(db, patient):
    booking = booking_in(db, patient, 5, BookingStatus.PENDING)  # looking for a caregiver, a few hours out
    caregiver = make_caregiver(db)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    accept = lambda s: BookingService(s).handle_booking_action(booking.id, caregiver.id, BookingActionRequest(action="accept"))  # noqa: E731
    # the family saw "free" (pending): the fee they accepted is 0
    out = race(("cancel", do_cancel(patient.user, booking, accepted_fee_kobo=0)), ("accept", accept))
    row = reload(db, booking)
    if isinstance(out["cancel"], cancel_service.Result):   # the cancel got the lock first
        assert row.status == BookingStatus.CANCELLED and row.caregiver_id is None and not isinstance(out["accept"], Booking)
        assert [r.suggested_amount_kobo for r in refunds(db, booking)] == [TOTAL]
    else:                                                  # the accept won: the 20% fee now applies, so nothing was cancelled
        assert isinstance(out["accept"], Booking) and isinstance(out["cancel"], cancel_service.PatientCancelError)
        assert out["cancel"].code == "terms_changed"
        assert row.status == BookingStatus.ACCEPTED and row.caregiver_id == caregiver.id and not refunds(db, booking)
    assert not (refunds(db, booking) and row.status != BookingStatus.CANCELLED)


def test_cancel_racing_the_caregiver_starting_the_visit_never_leaves_a_refunded_booking_in_progress(db, patient):
    booking = booking_in(db, patient, 30)
    caregiver_id, plain = booking.caregiver_id, decrypt(booking.visit_code_enc)
    start = lambda s: BookingService(s).handle_booking_action(booking.id, caregiver_id, BookingActionRequest(action="start", visit_code=plain))  # noqa: E731
    out = race(("cancel", do_cancel(patient.user, booking)), ("start", start))
    row = reload(db, booking)
    if isinstance(out["cancel"], cancel_service.Result):
        assert row.status == BookingStatus.CANCELLED and not isinstance(out["start"], Booking)
        assert len(refunds(db, booking)) == 1
    else:
        assert isinstance(out["start"], Booking) and out["cancel"].code == "booking_in_progress"
        assert row.status == BookingStatus.IN_PROGRESS and not refunds(db, booking) and not row.refund_required


# --- the two orders of each race, one at a time (the threaded runs above pick one of them at random) ----------------
def offered(db, booking):
    caregiver = make_caregiver(db)
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="t", message="m",
                               notification_type="new_booking"))
    db.commit()
    return caregiver


def test_accept_first_then_cancel_on_the_old_terms_changes_nothing(db, client, patient):
    booking = booking_in(db, patient, 5, BookingStatus.PENDING)
    caregiver = offered(db, booking)
    BookingService(db).handle_booking_action(booking.id, caregiver.id, BookingActionRequest(action="accept"))
    response = cancel(client, patient.user, booking, accepted_fee_kobo=0)   # the family still has the "free" screen open
    assert (response.status_code, code(response)) == (409, "terms_changed")
    assert reload(db, booking).status == BookingStatus.ACCEPTED and not refunds(db, booking)


def test_cancel_first_then_accept_is_refused_and_the_booking_is_not_reopened(db, client, patient):
    booking = booking_in(db, patient, 5, BookingStatus.PENDING)
    caregiver = offered(db, booking)
    assert cancel(client, patient.user, booking, accepted_fee_kobo=0).status_code == 200
    with pytest.raises(Exception) as refused:
        BookingService(db).handle_booking_action(booking.id, caregiver.id, BookingActionRequest(action="accept"))
    assert "no longer available" in str(refused.value)
    row = reload(db, booking)
    assert row.status == BookingStatus.CANCELLED and row.caregiver_id is None and len(refunds(db, booking)) == 1


def test_start_first_then_cancel_is_refused_and_no_refund_is_queued(db, client, patient):
    booking = booking_in(db, patient, 30)
    BookingService(db).handle_booking_action(booking.id, booking.caregiver_id,
                                             BookingActionRequest(action="start", visit_code=decrypt(booking.visit_code_enc)))
    response = cancel(client, patient.user, booking)
    assert (response.status_code, code(response)) == (409, "booking_in_progress")
    row = reload(db, booking)
    assert row.status == BookingStatus.IN_PROGRESS and not refunds(db, booking) and not row.refund_required


def test_cancel_first_then_start_is_refused(db, client, patient):
    booking = booking_in(db, patient, 30)
    plain, caregiver_id = decrypt(booking.visit_code_enc), booking.caregiver_id
    assert cancel(client, patient.user, booking).status_code == 200
    with pytest.raises(Exception) as refused:
        BookingService(db).handle_booking_action(booking.id, caregiver_id, BookingActionRequest(action="start", visit_code=plain))
    assert "can be started" in str(refused.value)
    assert reload(db, booking).status == BookingStatus.CANCELLED
