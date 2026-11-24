"""An app patient is e-mailed about the key booking events (the same outbox events as the feed row and push), once,
best effort, and never with an address, phone number, ID data, condition, notes or visit code in the body."""

import html as htmllib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta

import httpx
import pytest

from app.channels.whatsapp import notifications
from app.core.config import settings
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.notification import Notification
from app.db.models.refund import Refund
from app.db.models.user import User
from app.db.models.whatsapp_booking import BookingEvent
from app.services import booking_events, dispatch, email as email_service, patient_email, patient_notifications
from app.services.booking_payment import settle_booking_payment
from app.services.notification_service import NotificationService
from app.tests.factories import make_booking, make_caregiver, make_patient

CODE = "7391"  # the visit code: must never appear in an e-mail
SECRETS = ("Placeholder-Condition-HIV", "Gate code 0000", "12 Placeholder Street, Lekki", "+2348000000000",
           "Placeholder-ID-0000", CODE)


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)


class Outbox:
    """The faked Resend API: every request recorded, ``status`` makes it answer with an error."""

    def __init__(self):
        self.requests, self.status = [], 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"name": "validation_error", "message": "nope"})
        return httpx.Response(200, json={"id": "email-id-placeholder"})

    @property
    def mails(self):
        out = []
        for request in self.requests:
            body = json.loads(request.content)
            out.append({"to": body["to"], "subject": body["subject"], "html": body["html"],
                        "text": htmllib.unescape(re.sub(r"<[^>]+>", " ", body["html"]))})
        return out


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(box.handler))
    monkeypatch.setattr(settings, "RESEND_KEY", "re_placeholder_not_a_secret")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")
    monkeypatch.setattr(settings, "EMAILS_FROM_NAME", "Oakly")
    return box


@pytest.fixture
def world(db, expo, outbox):
    patient = make_patient(db)
    user = db.get(User, patient.user_id)
    user.full_name = "Adaeze Placeholder-Surname"
    db.commit()
    return {"patient": patient, "user": user, "caregiver": make_caregiver(db, full_name="Ngozi Okafor")}


def booking_for(db, world, caregiver=False, **kw):
    kw.setdefault("visit_code", CODE)
    kw.setdefault("condition", "Placeholder-Condition-HIV")
    kw.setdefault("special_instructions", "Gate code 0000")
    kw.setdefault("patient_address", "12 Placeholder Street, Lekki")
    return make_booking(db, world["patient"], world["caregiver"] if caregiver else None, **kw)


def run(db):
    return notifications.deliver_pending(db)


def assert_clean(mail, world, booking):
    """The booker's address, a first-name greeting, the booking reference, and none of the forbidden data."""
    assert mail["to"] == [world["user"].email]
    assert "Hello Adaeze," in mail["text"] and "Placeholder-Surname" not in mail["text"]
    assert booking.booking_code in mail["text"]
    for secret in SECRETS + (world["user"].phone_number,):
        assert secret not in mail["html"] and secret not in mail["subject"]


# --- one e-mail per key event ------------------------------------------------------------------------------
def test_payment_received_sends_the_receipt_with_the_stored_amount_and_reference(db, world, outbox):
    booking = booking_for(db, world, payment_status=PaymentStatus.PENDING, total_amount_kobo=4_400_050)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    run(db)
    db.refresh(booking)
    [mail] = outbox.mails
    assert mail["subject"] == f"{settings.PROJECT_NAME} - Booking confirmed: payment received"
    assert_clean(mail, world, booking)
    for fact in ("Nurse", "4 hours", "₦44,000.50", "Nigerian naira (NGN)", booking.paystack_reference,
                 "finding a caregiver"):
        assert fact in mail["text"]
    start = booking.scheduled_date.replace(tzinfo=None)
    assert f"{start:%Y}" in mail["text"] and "(Lagos time)" in mail["text"]


def test_a_caregiver_assignment_names_the_caregiver_by_first_name_and_gives_the_time(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.ACCEPTED)
    NotificationService(db).caregiver_accepted(booking)
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert mail["subject"] == f"{settings.PROJECT_NAME} - Your caregiver has been assigned"
    assert_clean(mail, world, booking)
    assert "Ngozi will be your caregiver." in mail["text"] and "Okafor" not in mail["text"]
    assert "(Lagos time)" in mail["text"]
    assert world["caregiver"].phone_number not in mail["html"]


def test_a_reassignment_says_the_new_caregiver_now_comes(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.ACCEPTED)
    NotificationService(db).caregiver_reassigned(booking)
    db.commit()
    run(db)
    [mail] = outbox.mails  # visit_code_issued is not an app event: one mail
    assert mail["subject"] == f"{settings.PROJECT_NAME} - Your caregiver has changed"
    assert "Ngozi will now be your caregiver." in mail["text"]
    assert_clean(mail, world, booking)


def test_a_completed_visit_gets_a_short_thank_you_with_the_feed_line(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert mail["subject"] == f"{settings.PROJECT_NAME} - Your visit is complete"
    assert "Your visit is complete." in mail["text"] and "Thank you" in mail["text"]
    assert_clean(mail, world, booking)
    for banned in ("vital", "note", "care plan"):
        assert banned not in mail["text"].lower()


def test_a_caregiver_cancelling_after_payment_says_so_and_names_the_refund_amount(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.CANCELLED, refund_required=True,
                          refund_amount_kobo=2_200_000)
    NotificationService(db).cancelled_by_caregiver(booking)
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert mail["subject"] == f"{settings.PROJECT_NAME} - Your booking was cancelled"
    assert "Your caregiver can no longer attend" in mail["text"] and "₦22,000" in mail["text"]
    assert "refund" in mail["text"]
    assert_clean(mail, world, booking)


def test_oakly_cancelling_says_so_and_a_refund_line_only_when_one_follows(db, world, outbox):
    with_refund = booking_for(db, world, status=BookingStatus.CANCELLED, refund_required=True)
    without = booking_for(db, world, status=BookingStatus.CANCELLED)
    for booking in (with_refund, without):
        NotificationService(db).cancelled_by_admin(booking)
    db.commit()
    run(db)
    assert len(outbox.mails) == 2
    first = next(m for m in outbox.mails if with_refund.booking_code in m["text"])
    second = next(m for m in outbox.mails if without.booking_code in m["text"])
    assert "Oakly has cancelled this visit." in first["text"] and "₦44,000" not in first["text"]  # no amount decided yet
    assert "Our team will contact you about your refund." in first["text"]
    assert "Oakly has cancelled this visit." in second["text"] and "refund" not in second["text"].lower()


def test_a_visit_cancelled_before_it_was_paid_sends_nothing(db, world, outbox):
    booking = booking_for(db, world, status=BookingStatus.CANCELLED, payment_status=PaymentStatus.PENDING)
    NotificationService(db).cancelled_by_admin(booking)
    db.commit()
    run(db)
    assert outbox.requests == []


def test_no_caregiver_found_sends_one_notice(db, world, outbox):
    booking = booking_for(db, world)
    NotificationService(db).no_caregiver_found(booking)
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert mail["subject"] == f"{settings.PROJECT_NAME} - We're still looking for a caregiver"
    assert "couldn't find a caregiver yet" in mail["text"]
    assert_clean(mail, world, booking)


def test_a_sent_refund_gives_the_refund_row_amount_and_where_it_goes(db, world, outbox):
    booking = booking_for(db, world, status=BookingStatus.CANCELLED, payment_status=PaymentStatus.REFUNDED)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=3_000_000, amount_kobo=3_000_000, destination="original_method",
                    status="succeeded", approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    booking_events.emit(db, booking, "refund_processed", f"{refund.id}:complete")
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert mail["subject"] == f"{settings.PROJECT_NAME} - Your refund has been sent"
    assert "₦30,000" in mail["text"] and "₦44,000" not in mail["text"]  # the refund row, not the booking total
    assert "card or bank account you paid with" in mail["text"]
    assert_clean(mail, world, booking)


def test_a_wallet_refund_says_the_wallet(db, world, outbox):
    booking = booking_for(db, world, status=BookingStatus.CANCELLED, payment_status=PaymentStatus.REFUNDED)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=100_000, amount_kobo=100_000, destination="wallet", status="succeeded",
                    approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    booking_events.emit(db, booking, "refund_processed", f"{refund.id}:complete")
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert "₦1,000" in mail["text"] and "Oakly wallet" in mail["text"]


# --- events that are push-only ---------------------------------------------------------------------------
def test_on_the_way_arrived_started_and_refund_started_send_no_email(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.IN_PROGRESS)
    svc = NotificationService(db)
    svc.caregiver_on_the_way(booking)
    svc.caregiver_arrived(booking)
    svc.visit_started(booking)
    svc.refund_flagged(booking)
    booking_events.emit(db, booking, "refund_needs_bank_details", f"{uuid.uuid4()}:needs_attention")
    db.commit()
    run(db)
    db.expire_all()
    # the feed was still written for the events that apply (started, and refund started / bank details)
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() >= 1
    assert outbox.requests == []
    assert set(patient_email.EMAILED_EVENTS).isdisjoint(
        {"caregiver_en_route", "caregiver_arrived", "visit_started", "refund_flagged", "refund_needs_bank_details"})


# --- who is, and is not, e-mailed --------------------------------------------------------------------------
def test_a_whatsapp_booking_gets_no_patient_email(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED, channel="whatsapp",
                          booked_by_user_id=world["user"].id)
    NotificationService(db).visit_completed(booking)
    booking_events.emit(db, booking, "payment_received")
    db.commit()
    run(db)
    assert outbox.requests == []
    # and the helper itself refuses a WhatsApp booking even if called directly
    event = db.query(BookingEvent).filter_by(booking_id=booking.id, event="visit_completed").one()
    assert patient_email.send_for_event(db, world["user"].id, event, booking, "visit_completed",
                                        booking.booking_code) is False
    assert outbox.requests == []


def test_a_user_without_an_email_gets_the_feed_row_and_no_email(db, world, outbox):
    world["user"].email = None
    db.commit()
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    assert outbox.requests == []
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 1


def test_an_erased_account_gets_nothing(db, world, outbox):
    world["user"].is_active = False
    db.commit()
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    assert outbox.requests == []


# --- best effort, once -------------------------------------------------------------------------------------
def test_a_failing_transport_never_breaks_delivery_and_logs_only_the_kind(db, world, outbox, caplog):
    outbox.status = 500
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    with caplog.at_level(logging.INFO):
        run(db)
    db.expire_all()
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).one()
    assert event.delivered_at is not None  # the event is finished: the feed row is the record
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 1
    assert len(outbox.requests) == 1
    assert "patient e-mail visit_completed not sent" in caplog.text
    assert world["user"].email not in caplog.text


def test_a_transport_that_raises_never_breaks_delivery(db, world, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(boom))
    monkeypatch.setattr(settings, "RESEND_KEY", "re_placeholder_not_a_secret")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 1


def test_a_rendering_or_composing_error_is_swallowed(db, world, outbox, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(email_service, "render_template", boom)
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    assert outbox.requests == []
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 1
    assert db.query(Booking).filter(Booking.id == booking.id).one().status == BookingStatus.COMPLETED


def test_email_not_configured_is_a_quiet_no_op(db, world, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_KEY", "")
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 1


def test_a_replayed_event_does_not_send_twice(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    assert len(outbox.requests) == 1
    db.expire_all()
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).one()
    for _ in range(2):  # the worker re-delivers the same outbox row (a crash before it was marked delivered)
        assert patient_notifications.deliver(db, event, db.get(Booking, booking.id)) in ("sent", "no_device")
    run(db)
    assert len(outbox.requests) == 1
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 1


def test_a_feed_write_failure_sends_no_email_then_one_on_the_retry(db, world, outbox, monkeypatch):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    real = patient_notifications._write_feed_row
    monkeypatch.setattr(patient_notifications, "_write_feed_row",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    run(db)
    assert outbox.requests == []  # nothing is told about before the feed row exists
    monkeypatch.setattr(patient_notifications, "_write_feed_row", real)
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).one()
    event.claimed_at = None
    db.commit()
    run(db)
    assert len(outbox.requests) == 1


def test_the_email_goes_out_after_the_feed_row_is_committed(db, world, outbox, monkeypatch):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    seen = {}
    real = patient_email.send_for_event

    def spy(session, *args, **kwargs):
        from app.db.session import SessionLocal  # a separate connection sees only committed rows
        with SessionLocal() as other:
            seen["rows"] = other.query(Notification).filter(Notification.user_id == world["user"].id).count()
        return real(session, *args, **kwargs)

    monkeypatch.setattr(patient_email, "send_for_event", spy)
    run(db)
    assert seen == {"rows": 1} and len(outbox.requests) == 1


def test_an_unverified_address_is_never_mailed_and_a_verified_one_is(db, world, outbox):
    world["user"].is_verified = False
    db.commit()
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    run(db)
    assert outbox.requests == []  # a mistyped or stranger's address would get a receipt
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 1  # feed still written

    world["user"].is_verified = True
    other = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(other)
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert mail["to"] == [world["user"].email] and other.booking_code in mail["text"]


def test_a_cancel_with_no_decided_refund_amount_never_quotes_the_total(db, world, outbox):
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.CANCELLED, refund_required=True,
                          total_amount_kobo=7_700_000)
    NotificationService(db).cancelled_by_caregiver(booking)
    db.commit()
    run(db)
    [mail] = outbox.mails
    assert "Our team will contact you about your refund." in mail["text"]
    assert "₦77,000" not in mail["text"] and "We've started a refund" not in mail["text"]


def test_a_refund_that_is_no_longer_succeeded_is_not_announced_as_sent(db, world, outbox):
    booking = booking_for(db, world, status=BookingStatus.CANCELLED, payment_status=PaymentStatus.REFUNDED)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=3_000_000, amount_kobo=3_000_000, destination="original_method",
                    status="needs_review", approved_at=datetime.utcnow())
    db.add(refund)
    db.commit()
    booking_events.emit(db, booking, "refund_processed", f"{refund.id}:complete")
    db.commit()
    run(db)
    assert outbox.requests == []
    assert db.query(Notification).filter(Notification.user_id == world["user"].id).count() == 0  # the feed row too


def test_the_template_escapes_every_value(db):
    html = email_service.render_template("patient_booking", {
        "project_name": "<b>Brand</b>", "heading": "<script>x</script>", "first_name": '<a href="x">Eve</a>',
        "paragraphs": ("<img src=x onerror=alert(1)>",), "facts": (("<i>L</i>", "<u>V</u>"),),
        "booking_code": "<code>"})
    for raw in ('<a href="x">', "<script>", "<img src=x", "<i>L</i>", "<u>V</u>", "<code>", "<b>Brand</b>"):
        assert raw not in html
    assert "&lt;a href=&#34;x&#34;&gt;Eve&lt;/a&gt;" in html


def test_a_failed_send_logs_the_status_and_error_name_but_never_the_address(db, world, monkeypatch, caplog):
    address = world["user"].email

    def handler(request):
        return httpx.Response(422, json={"name": "validation_error", "message": f"Invalid `to` field: {address}"})

    monkeypatch.setattr(email_service, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "RESEND_KEY", "re_placeholder_not_a_secret")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@example.test")
    booking = booking_for(db, world, caregiver=True, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    with caplog.at_level(logging.INFO):
        run(db)
    assert "422" in caplog.text and "validation_error" in caplog.text
    assert address not in caplog.text
