"""P8-BE: an app patient hears about their booking in the app (feed row + Expo push), independent of WhatsApp."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.channels.whatsapp import notifications
from app.core.security import decrypt, encrypt
from app.db.models.booking import Booking, BookingNotification, BookingStatus, PaymentStatus
from app.db.models.notification import Notification
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.db.models.whatsapp_booking import BookingEvent
from app.schemas.booking import BookingActionRequest
from app.services import booking_events, dispatch, patient_notifications
from app.services.booking import BookingService
from app.services.booking_payment import settle_booking_payment
from app.services.notification_service import NotificationService
from app.tests.factories import auth_headers, make_booking, make_caregiver, make_patient

FEED = "/api/v1/patient/notifications"
CODE = "7391"  # a visit code that must never reach a lock screen


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)


def user_of(db, patient) -> User:
    return db.get(User, patient.user_id)


def with_token(db, user, name="patient"):
    token = f"ExponentPushToken[{name}-{user.id.hex[:8]}]"
    db.add(PushToken(user_id=user.id, token=token, platform="ios"))
    db.commit()
    return token


def feed(db, user):
    db.expire_all()
    return (db.query(Notification).filter(Notification.user_id == user.id)
            .order_by(Notification.created_at, Notification.id).all())


def deliver(db):
    return notifications.deliver_pending(db)


def app_booking(db, patient, caregiver=None, **kw):
    kw.setdefault("visit_code", CODE)
    return make_booking(db, patient, caregiver, **kw)


def offer(db, booking, caregiver):
    db.add(BookingNotification(booking_id=booking.id, caregiver_id=caregiver.id, title="New Booking Request",
                               message="x", notification_type="new_booking"))
    db.commit()


def act(db, booking, caregiver, action, **extra):
    return BookingService(db).handle_booking_action(
        booking.id, caregiver.id, BookingActionRequest(action=action, **extra))


def events(db, booking):
    db.expire_all()
    return [e.event for e in db.query(BookingEvent).filter_by(booking_id=booking.id).order_by(BookingEvent.created_at)]


@pytest.fixture
def world(db, expo):
    """A patient with a device, a bystander with a device, and an approved caregiver."""
    patient, other = make_patient(db), make_patient(db)
    caregiver = make_caregiver(db, full_name="Ngozi Okafor")
    return {
        "patient": patient, "user": user_of(db, patient), "token": with_token(db, user_of(db, patient)),
        "other": other, "other_user": user_of(db, other), "other_token": with_token(db, user_of(db, other), "other"),
        "caregiver": caregiver,
    }


def lock_screen_text(expo) -> str:
    """Everything a lock screen can show: the title and body of every push."""
    return " | ".join(f"{m['title']} {m['body']}" for m in expo.messages)


# --- the journey, through the real booking transitions ---------------------------------------------------
def test_each_step_of_a_visit_writes_exactly_one_feed_row_for_the_patient_only(db, expo, world):
    start = datetime.utcnow() + timedelta(minutes=10)
    booking = app_booking(db, world["patient"], scheduled_date=start, scheduled_end_date=start + timedelta(hours=4))
    offer(db, booking, world["caregiver"])

    act(db, booking, world["caregiver"], "accept")
    assert events(db, booking) == ["caregiver_assigned"]  # the app shows the visit code: no visit_code_issued
    deliver(db)  # the worker runs every 15 seconds: each step is told while it is still true
    act(db, booking, world["caregiver"], "on_my_way")
    deliver(db)
    visit_code = decrypt(db.get(Booking, booking.id).visit_code_enc)
    act(db, booking, world["caregiver"], "start", visit_code=visit_code)
    deliver(db)
    act(db, booking, world["caregiver"], "complete")
    deliver(db)
    deliver(db)  # a second pass has nothing left to do

    rows = feed(db, world["user"])
    assert [r.type for r in rows] == ["caregiver_found", "caregiver_on_the_way", "visit_started", "visit_completed"]
    assert rows[0].body == "Ngozi will be your caregiver."
    assert rows[1].body == "Ngozi is on the way."
    assert all(r.data == {"booking_id": str(booking.id), "target": "booking_detail"} for r in rows)
    assert feed(db, world["other_user"]) == [] and feed(db, make_caregiver_user(db, world)) == []
    assert [m["to"] for m in expo.messages] == [world["token"]] * 4  # only the patient's device
    assert [m["data"]["type"] for m in expo.messages] == [r.type for r in rows]
    assert {m["data"]["notification_id"] for m in expo.messages} == {str(r.id) for r in rows}
    assert db.get(Booking, booking.id).status == BookingStatus.COMPLETED


def make_caregiver_user(db, world):
    return db.get(User, world["caregiver"].user_id)


def test_payment_received_confirms_the_booking(db, expo, world):
    booking = app_booking(db, world["patient"], payment_status=PaymentStatus.PENDING)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    deliver(db)
    [row] = feed(db, world["user"])
    assert (row.type, row.title) == ("booking_confirmed", "Booking confirmed")
    assert expo.messages[0]["title"] == "Booking confirmed" and feed(db, world["other_user"]) == []


def test_a_caregiver_cancelling_tells_the_patient_it_is_cancelled(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.ACCEPTED)
    act(db, booking, world["caregiver"], "cancel")
    deliver(db)
    [row] = feed(db, world["user"])
    assert (row.type, row.body) == ("booking_cancelled",
                                    "Your caregiver can no longer attend. Open the app for details.")
    assert len(expo.messages) == 1 and feed(db, world["other_user"]) == []


def test_oakly_cancelling_tells_the_patient(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.CANCELLED)
    NotificationService(db).cancelled_by_admin(booking)
    db.commit()
    deliver(db)
    [row] = feed(db, world["user"])
    assert row.type == "booking_cancelled" and row.body.startswith("Oakly has cancelled this visit.")


def test_no_caregiver_found_when_dispatch_gives_up(db, expo, world):
    now = datetime(2026, 10, 5, 12, 0, 0)
    booking = app_booking(db, world["patient"], dispatch_state="dispatching", dispatch_started_at=now)
    from app.core.config import settings
    dispatch.dispatch_tick(db, now + timedelta(minutes=settings.DISPATCH_ESCALATE_MINUTES + 1))
    assert db.get(Booking, booking.id).dispatch_state == "unmatched"
    deliver(db)
    [row] = feed(db, world["user"])
    assert row.type == "no_caregiver_found" and "couldn't find a caregiver" in row.body
    assert len(expo.messages) == 1


def test_refund_started_completed_and_needs_bank_details(db, expo, world):
    booking = app_booking(db, world["patient"], status=BookingStatus.CANCELLED, refund_required=True)
    NotificationService(db).refund_flagged(booking)
    NotificationService(db).refund_processed(booking)
    booking_events.emit(db, booking, "refund_needs_bank_details", f"{uuid.uuid4()}:needs_attention")
    db.commit()
    deliver(db)
    assert sorted(r.type for r in feed(db, world["user"])) == [
        "refund_completed", "refund_needs_bank_details", "refund_started"]
    assert len(expo.messages) == 3


def test_a_reassignment_names_the_new_caregiver(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.ACCEPTED)
    NotificationService(db).caregiver_reassigned(booking)
    db.commit()
    deliver(db)
    [row] = feed(db, world["user"])
    assert (row.type, row.body) == ("caregiver_changed", "Ngozi will now be your caregiver.")


# --- arrival, and the lock screen --------------------------------------------------------------------
def test_arrival_says_to_open_the_app_for_the_code_and_never_carries_it(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.EN_ROUTE)
    NotificationService(db).caregiver_arrived(booking)
    db.commit()
    deliver(db)
    [row] = feed(db, world["user"])
    assert (row.type, row.title, row.body) == (
        "caregiver_arrived", "Your caregiver has arrived", "Open the app to see your visit code.")
    assert expo.messages[0]["body"] == row.body


def test_lock_screen_text_never_holds_the_code_address_condition_or_booking_code(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.EN_ROUTE,
                          condition="Placeholder-Condition-HIV", special_instructions="Gate code 0000",
                          patient_address="12 Placeholder Street, Lekki")
    svc = NotificationService(db)
    svc.caregiver_accepted(booking)
    svc.caregiver_on_the_way(booking)
    svc.caregiver_arrived(booking)
    svc.visit_started(booking)  # not yet true of the booking: stale, dropped
    booking.status = BookingStatus.CANCELLED
    svc.cancelled_by_caregiver(booking)
    svc.refund_flagged(booking)
    booking_events.emit(db, booking, "payment_received")
    db.commit()
    deliver(db)
    texts = " | ".join(f"{r.title} {r.body}" for r in feed(db, world["user"])) + " | " + lock_screen_text(expo)
    for secret in (CODE, "Placeholder-Condition", "HIV", "Gate code", "Placeholder Street", "Lekki",
                   booking.booking_code, booking.patient.email, booking.patient.phone_number or "no-phone"):
        assert secret not in texts, secret
    assert CODE not in expo.raw_bodies.replace(str(booking.id), "")  # not even in the payload's data


# --- stale events, failures, retries -----------------------------------------------------------------------
def test_a_replaced_caregiver_is_not_announced(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.ACCEPTED)
    NotificationService(db).caregiver_accepted(booking)
    db.commit()
    booking.caregiver_id = make_caregiver(db).id  # replaced before the worker ran
    db.commit()
    deliver(db)
    assert feed(db, world["user"]) == [] and expo.messages == []
    assert db.query(BookingEvent).filter_by(booking_id=booking.id).one().delivered_at is not None  # nothing to retry


def test_a_failing_push_still_leaves_the_feed_row(db, expo, world):
    expo.status_code = 500
    booking = app_booking(db, world["patient"], payment_status=PaymentStatus.PENDING)
    settle_booking_payment(db, booking.id, booking.total_amount_kobo)
    deliver(db)
    assert [r.type for r in feed(db, world["user"])] == ["booking_confirmed"]
    assert db.query(BookingEvent).filter_by(booking_id=booking.id).one().delivered_at is not None


def test_a_push_that_raises_still_leaves_the_feed_row(db, expo, world):
    import httpx
    expo.raise_error = httpx.ConnectError("down")
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    deliver(db)
    assert [r.type for r in feed(db, world["user"])] == ["visit_completed"]


def test_a_patient_with_no_device_still_gets_the_feed_row(db, expo):
    patient = make_patient(db)
    booking = app_booking(db, patient, status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    deliver(db)
    assert [r.type for r in feed(db, user_of(db, patient))] == ["visit_completed"] and expo.messages == []


def test_an_unregistered_token_is_cleaned_up_and_the_row_is_kept(db, expo, world):
    live = with_token(db, world["user"], "second")
    expo.not_registered.add(world["token"])
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    deliver(db)
    db.expire_all()
    assert {t.token for t in db.query(PushToken).filter_by(user_id=world["user"].id)} == {live}
    assert db.query(PushToken).filter_by(token=world["other_token"]).count() == 1  # nobody else's token touched
    assert len(feed(db, world["user"])) == 1


def test_a_feed_write_failure_is_retried_and_never_double_writes(db, expo, world, monkeypatch):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    real = patient_notifications._write_feed_row
    monkeypatch.setattr(patient_notifications, "_write_feed_row",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    deliver(db)
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).one()
    assert feed(db, world["user"]) == [] and expo.messages == []  # nothing half-done, nothing pushed
    assert event.delivered_at is None and event.last_error == "feed_failed" and event.attempts == 1

    monkeypatch.setattr(patient_notifications, "_write_feed_row", real)
    event.claimed_at = None  # the claim lease has run out
    db.commit()
    deliver(db)
    assert len(feed(db, world["user"])) == 1 and len(expo.messages) == 1

    db.expire_all()  # delivering the same outbox row again (a crash after the commit) cannot add a second row
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).one()
    assert patient_notifications.deliver(db, event, db.get(Booking, booking.id)) == "sent"
    assert len(feed(db, world["user"])) == 1


def test_recording_a_notice_can_never_fail_the_booking_action(db, expo, world, monkeypatch):
    booking = app_booking(db, world["patient"])
    offer(db, booking, world["caregiver"])

    def boom(*a, **k):
        raise RuntimeError("outbox down")

    monkeypatch.setattr(booking_events, "_insert", boom)
    result = act(db, booking, world["caregiver"], "accept")  # does not raise
    assert result.status == BookingStatus.ACCEPTED and result.caregiver_id == world["caregiver"].id
    db.expire_all()
    assert db.get(Booking, booking.id).status == BookingStatus.ACCEPTED  # committed
    assert events(db, booking) == [] and feed(db, world["user"]) == []


# --- channels do not mix --------------------------------------------------------------------------------------
def test_a_whatsapp_booking_gets_no_feed_row_and_no_push(db, expo, world):
    booking = make_booking(db, world["patient"], world["caregiver"], status=BookingStatus.COMPLETED,
                           channel="whatsapp", booked_by_user_id=world["user"].id)
    svc = NotificationService(db)
    svc.visit_completed(booking)
    svc.caregiver_arrived(booking)  # app-only events are never recorded for WhatsApp
    svc.no_caregiver_found(booking)
    assert booking_events.emit(db, booking, "payment_received") is True  # its WhatsApp notices are unchanged
    db.commit()
    assert sorted(events(db, booking)) == ["payment_received", "visit_completed"]
    deliver(db)
    assert feed(db, world["user"]) == [] and expo.messages == []
    errors = {e.event: e.last_error for e in db.query(BookingEvent).filter_by(booking_id=booking.id)}
    assert errors == {"payment_received": "no_contact", "visit_completed": "no_contact"}  # the WhatsApp path ran


def test_app_booking_events_are_limited_to_the_app_list(db):
    booking = app_booking(db, make_patient(db))
    assert booking_events.emit(db, booking, "visit_code_issued") is False  # the app shows the code
    assert booking_events.emit(db, booking, "payment_link_expiring") is False  # a WhatsApp journey
    assert booking_events.emit(db, booking, "dropout_choice_needed", "x") is False
    assert booking_events.emit(db, booking, "payment_received") is True
    assert booking_events.emit(db, booking, "payment_received") is False  # once


def test_the_admin_channel_is_not_told_in_the_app(db):
    booking = app_booking(db, make_patient(db), channel="admin")
    assert booking_events.emit(db, booking, "payment_received") is False


# --- the feed endpoints ------------------------------------------------------------------------------------------
def seed_feed(db, user, n, start=None):
    start = start or datetime.utcnow() - timedelta(hours=n)
    rows = []
    for i in range(n):
        row = Notification(user_id=user.id, type="visit_started", title=f"t{i}", body=f"b{i}",
                           data={"booking_id": str(uuid.uuid4()), "target": "booking_detail"},
                           created_at=start + timedelta(minutes=i))
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def test_the_feed_is_newest_first_paginated_and_counts_unread_across_pages(db, client):
    user = user_of(db, make_patient(db))
    rows = seed_feed(db, user, 5)
    rows[4].read_at = datetime.utcnow()  # the newest is already read
    db.commit()

    first = client.get(FEED, params={"limit": 2}, headers=auth_headers(user)).json()
    assert [i["title"] for i in first["items"]] == ["t4", "t3"]
    assert (first["total"], first["limit"], first["offset"], first["unread_count"]) == (5, 2, 0, 4)
    second = client.get(FEED, params={"limit": 2, "offset": 2}, headers=auth_headers(user)).json()
    assert [i["title"] for i in second["items"]] == ["t2", "t1"]
    last = client.get(FEED, params={"limit": 2, "offset": 4}, headers=auth_headers(user)).json()
    assert [i["title"] for i in last["items"]] == ["t0"]
    assert set(first["items"][0]) == {"id", "type", "title", "body", "data", "read_at", "created_at"}
    assert first["items"][0]["read_at"] is not None and first["items"][1]["read_at"] is None


def test_an_empty_feed(db, client):
    user = user_of(db, make_patient(db))
    body = client.get(FEED, headers=auth_headers(user)).json()
    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0, "unread_count": 0}


def test_a_patient_only_ever_sees_their_own_rows(db, client):
    mine, theirs = user_of(db, make_patient(db)), user_of(db, make_patient(db))
    seed_feed(db, mine, 2)
    [other_row] = seed_feed(db, theirs, 1)
    body = client.get(FEED, headers=auth_headers(mine)).json()
    assert body["total"] == 2 and other_row.id not in {uuid.UUID(i["id"]) for i in body["items"]}
    denied = client.post(f"{FEED}/{other_row.id}/read", headers=auth_headers(mine))
    assert denied.status_code == 404 and denied.json() == {"detail": "Notification not found."}
    db.expire_all()
    assert db.get(Notification, other_row.id).read_at is None  # and it was not touched
    client.post(f"{FEED}/read-all", headers=auth_headers(mine))
    db.expire_all()
    assert db.get(Notification, other_row.id).read_at is None  # read-all is scoped too


def test_reading_one_is_idempotent_and_keeps_the_first_read_time(db, client):
    user = user_of(db, make_patient(db))
    rows = seed_feed(db, user, 2)
    url = f"{FEED}/{rows[0].id}/read"
    first = client.post(url, headers=auth_headers(user))
    assert first.status_code == 200 and first.json()["read_at"] is not None
    again = client.post(url, headers=auth_headers(user))
    assert again.status_code == 200 and again.json()["read_at"] == first.json()["read_at"]
    assert client.get(FEED, headers=auth_headers(user)).json()["unread_count"] == 1
    assert client.post(f"{FEED}/{uuid.uuid4()}/read", headers=auth_headers(user)).status_code == 404


def test_read_all_is_idempotent(db, client):
    user = user_of(db, make_patient(db))
    seed_feed(db, user, 3)
    first = client.post(f"{FEED}/read-all", headers=auth_headers(user))
    assert first.status_code == 200 and first.json() == {"marked_read": 3, "unread_count": 0}
    assert client.post(f"{FEED}/read-all", headers=auth_headers(user)).json() == {"marked_read": 0, "unread_count": 0}
    assert client.get(FEED, headers=auth_headers(user)).json()["unread_count"] == 0


def test_the_feed_is_for_patients_only_and_needs_a_sign_in(db, client):
    assert client.get(FEED).status_code == 401
    caregiver_user = db.get(User, make_caregiver(db).user_id)
    denied = client.get(FEED, headers=auth_headers(caregiver_user))
    assert denied.status_code == 403 and denied.json() == {"detail": "Access denied. Patient account required."}
    assert client.post(f"{FEED}/read-all", headers=auth_headers(caregiver_user)).status_code == 403


def test_bad_paging_and_ids_are_rejected(db, client):
    user = user_of(db, make_patient(db))
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get(FEED, params=params, headers=auth_headers(user)).status_code == 422
    assert client.post(f"{FEED}/not-a-uuid/read", headers=auth_headers(user)).status_code == 422


# --- the device token a patient registers is the one that gets the push --------------------------------------------
def test_a_patient_registers_a_device_and_receives_the_push(db, client, expo):
    patient = make_patient(db)
    user = user_of(db, patient)
    token = "ExponentPushToken[patient-device-abc]"
    ok = client.post("/api/v1/devices/push-token", json={"token": token, "platform": "ios"}, headers=auth_headers(user))
    assert ok.status_code == 200
    booking = app_booking(db, patient, make_caregiver(db), status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    deliver(db)
    assert [m["to"] for m in expo.messages] == [token]


def test_erasing_an_account_removes_its_feed(db, client):
    patient = make_patient(db)
    user = user_of(db, patient)
    seed_feed(db, user, 2)
    resp = client.request("DELETE", "/api/v1/auth/profile", headers=auth_headers(user))
    assert resp.status_code in (200, 204), resp.text
    db.expire_all()
    assert db.query(Notification).filter_by(user_id=user.id).count() == 0


# --- review follow-ups --------------------------------------------------------------------------------------------
def test_a_cancel_is_not_announced_for_a_payment_that_failed(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.CANCELLED,
                          payment_status=PaymentStatus.FAILED)
    NotificationService(db).cancelled_by_admin(booking)
    db.commit()
    deliver(db)
    assert feed(db, world["user"]) == [] and expo.messages == []


def test_an_erased_account_gets_no_new_feed_rows(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    world["user"].is_active = False
    db.commit()
    deliver(db)
    assert feed(db, world["user"]) == [] and expo.messages == []


def test_no_caregiver_found_is_told_again_after_a_new_dispatch_run(db, expo, world):
    booking = app_booking(db, world["patient"], dispatch_started_at=datetime(2026, 10, 5, 12, 0))
    notify = NotificationService(db)
    notify.no_caregiver_found(booking)
    notify.no_caregiver_found(booking)  # the same run: once
    booking.dispatch_started_at = datetime(2026, 10, 5, 15, 0)  # an admin re-dispatched
    notify.no_caregiver_found(booking)
    db.commit()
    assert events(db, booking) == ["no_caregiver_found", "no_caregiver_found"]


def test_the_feed_row_is_dated_when_the_event_happened(db, expo, world):
    booking = app_booking(db, world["patient"], world["caregiver"], status=BookingStatus.COMPLETED)
    NotificationService(db).visit_completed(booking)
    db.commit()
    event = db.query(BookingEvent).filter_by(booking_id=booking.id).one()
    deliver(db)
    [row] = feed(db, world["user"])
    assert row.created_at == event.created_at
