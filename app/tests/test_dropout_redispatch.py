"""A caregiver dropping out of a WhatsApp booking: automatic replacement, then a choice for the family."""

import json
import threading
from datetime import datetime, timedelta

import pytest

from app.channels.whatsapp import notifications
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingDispatchWave, BookingNotification, BookingStatus, CaregiverLocation
from app.db.models.caregiver import CaregiverSkill
from app.db.models.dropout import CaregiverDropout
from app.db.models.push_token import PushToken
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.schemas.booking import BookingActionRequest
from app.services import dropout
from app.services.booking import BookingActionForbidden, BookingService
from app.tests.factories import make_booking, make_caregiver, make_patient
from app.tests.factories_whatsapp import open_tickets
from app.tests.fakes import expo  # noqa: F401  (fixture)
from app.tests.policy_helpers import set_policy
from app.tests.whatsapp_notify_helpers import (  # noqa: F401
    CONDITION, INSTRUCTIONS, Chat, accepted_booking, act, body_text, events_of, fake_meta, make_recipient, offer,
    onboarded, template_params, wa_booking,
)

LAT, LON = 6.4474, 3.4723


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def candidate(db, i=1):
    """An eligible caregiver near the patient (fresh location, nurse skill)."""
    cg = make_caregiver(db)
    db.add(PushToken(user_id=cg.user_id, token=f"ExponentPushToken[cand-{i}-{cg.id}]", platform="ios"))
    db.add(CaregiverSkill(caregiver_id=cg.id, service_type="nurse"))
    db.add(CaregiverLocation(caregiver_id=cg.id, latitude=LAT + 0.002 * i, longitude=LON, is_active=True,
                             is_available=True, updated_at=datetime.utcnow()))
    db.commit()
    return cg


def reload(db, booking):
    db.expire_all()
    return db.get(Booking, booking.id)


def deliver(db, chat):
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    return chat.fake.bodies[before:]


def drop_out(db, chat, *, hours_ahead, en_route=False, extra_candidates=2):
    """An accepted WhatsApp booking whose caregiver then cancels. Returns (booking, dropped caregiver, others)."""
    others = [candidate(db, i + 1) for i in range(extra_candidates)]
    booking, caregiver = accepted_booking(db, chat, hours_ahead=hours_ahead)
    # The one who drops is eligible too (skill, fresh location, push token): only the exclusion keeps them out.
    db.add(PushToken(user_id=caregiver.user_id, token=f"ExponentPushToken[dropper-{caregiver.id}]", platform="ios"))
    db.add(CaregiverSkill(caregiver_id=caregiver.id, service_type="nurse"))
    db.add(CaregiverLocation(caregiver_id=caregiver.id, latitude=LAT + 0.001, longitude=LON, is_active=True,
                             is_available=True, updated_at=datetime.utcnow()))
    db.commit()
    deliver(db, chat)  # the assignment messages
    if en_route:
        act(db, booking, caregiver, "on_my_way")
        deliver(db, chat)
    act(db, booking, caregiver, "cancel")
    return reload(db, booking), caregiver, others


def event_names(db, booking):
    return [e for e, _ in events_of(db, booking)]


def buttons(body):
    return [b["reply"]["id"] for b in body["interactive"]["action"]["buttons"]]


# --- the automatic replacement ---------------------------------------------------------------------------------
def test_a_dropout_well_before_the_visit_reopens_the_booking_and_redispatches_without_a_refund(db, chat):
    booking, caregiver, others = drop_out(db, chat, hours_ahead=5)
    assert booking.status == BookingStatus.PENDING and booking.caregiver_id is None
    assert booking.dispatch_state == "dispatching" and booking.dropout_state == "redispatching"
    assert booking.visit_code_enc is None and booking.cancel_reason is None
    assert not booking.refund_required and booking.refund_amount_kobo is None
    assert "booking_cancelled_by_caregiver" not in event_names(db, booking)


def test_the_dropped_caregiver_is_excluded_and_the_others_are_offered_the_visit(db, chat, expo):
    booking, caregiver, others = drop_out(db, chat, hours_ahead=5)
    wave = db.query(BookingDispatchWave).filter_by(booking_id=booking.id).order_by(
        BookingDispatchWave.wave_no.desc()).first()
    assert caregiver.id not in wave.notified_caregiver_ids
    assert set(wave.notified_caregiver_ids) == {c.id for c in others}
    offers = [m for m in expo.messages if m["data"]["type"] == "job_offer"]  # pushed after the commit
    assert len(offers) == 2 and all(str(caregiver.id) not in m["to"] for m in offers)


def test_the_dropped_caregiver_cannot_take_the_booking_back(db, chat):
    booking, caregiver, _ = drop_out(db, chat, hours_ahead=5)
    with pytest.raises(BookingActionForbidden):
        act(db, booking, caregiver, "accept")


def test_the_booker_is_told_without_health_data_and_nothing_to_do(db, chat):
    booking, _, _ = drop_out(db, chat, hours_ahead=5)
    sent = deliver(db, chat)
    assert len(sent) == 1
    text = body_text(sent[0])
    assert text == (f"Booking {booking.booking_code}: your professional had to cancel. We're finding you another "
                    "one, so there's nothing you need to do. Reply here if you need help.")
    assert CONDITION not in json.dumps(sent) and INSTRUCTIONS not in json.dumps(sent)


def test_outside_the_window_the_notice_is_the_template(db, chat):
    booking, caregiver, _ = drop_out(db, chat, hours_ahead=5)
    chat.contact.last_inbound_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    sent = deliver(db, chat)
    assert sent[0]["template"]["name"] == "dropout_replacing"
    assert template_params(sent[0]) == [booking.booking_code]


def test_when_a_replacement_accepts_the_normal_assignment_messages_fire_for_the_new_caregiver(db, chat):
    booking, caregiver, others = drop_out(db, chat, hours_ahead=5)
    deliver(db, chat)
    new = others[0]
    act(db, booking, new, "accept")
    b = reload(db, booking)
    assert b.caregiver_id == new.id and b.status == BookingStatus.ACCEPTED and b.dropout_state is None
    sent = deliver(db, chat)
    texts = [body_text(s) for s in sent]
    assert any("will visit on" in t for t in texts)
    assert any(decrypt(b.visit_code_enc) in t and "visit code" in t.lower() for t in texts)


def test_the_dropout_is_recorded_against_the_caregiver(db, chat):
    booking, caregiver, _ = drop_out(db, chat, hours_ahead=5)
    row = db.query(CaregiverDropout).filter_by(booking_id=booking.id).one()
    assert row.caregiver_id == caregiver.id and row.was_en_route is False
    assert 290 <= row.minutes_before_start <= 301
    assert dropout.dropout_count(db, caregiver.id) == 1


def test_an_app_booking_is_unchanged_but_the_dropout_is_still_recorded(db, chat):
    patient, caregiver = make_patient(db), make_caregiver(db)
    booking = make_booking(db, patient, caregiver, status=BookingStatus.ACCEPTED, visit_code="1234")
    act(db, booking, caregiver, "cancel")
    b = reload(db, booking)
    assert b.status == BookingStatus.CANCELLED and b.caregiver_id == caregiver.id and b.dropout_state is None
    assert not b.refund_required and events_of(db, b) == []
    assert dropout.dropout_count(db, caregiver.id) == 1


def test_a_mid_visit_cancel_is_not_redispatched(db, chat):
    booking, caregiver = accepted_booking(db, chat, hours_ahead=5)
    act(db, booking, caregiver, "start", visit_code=decrypt(booking.visit_code_enc))
    act(db, booking, caregiver, "cancel")
    b = reload(db, booking)
    assert b.status == BookingStatus.CANCELLED and b.refund_required and b.refund_amount_kobo is None
    assert b.dropout_state is None


# --- when a replacement is not tried: the choice ---------------------------------------------------------------
@pytest.mark.parametrize("hours_ahead, expected", [(2.1, "redispatching"), (1.9, "awaiting_choice")])
def test_the_two_hour_line_decides_between_replacement_and_choice(db, chat, hours_ahead, expected):
    booking, _, _ = drop_out(db, chat, hours_ahead=hours_ahead)
    assert booking.dropout_state == expected


def test_the_redispatch_lead_is_read_from_the_policy(db, chat):
    set_policy(db, dropout_redispatch_min_lead_minutes=600)
    booking, _, _ = drop_out(db, chat, hours_ahead=5)
    assert booking.dropout_state == "awaiting_choice"


def test_a_dropout_after_on_my_way_goes_straight_to_the_choice_even_far_ahead(db, chat):
    set_policy(db, onmyway_compensation_window_minutes=600)  # on my way is allowed 10 h ahead, well beyond the 2 h line
    booking, caregiver, _ = drop_out(db, chat, hours_ahead=9, en_route=True)
    assert booking.dropout_state == "awaiting_choice" and booking.status == BookingStatus.PENDING
    assert dropout.dropout_count(db, caregiver.id) == 1
    assert db.query(CaregiverDropout).one().was_en_route is True


def test_the_choice_message_has_three_buttons_and_no_fee_and_no_health_data(db, chat):
    booking, _, _ = drop_out(db, chat, hours_ahead=1.5)
    sent = deliver(db, chat)
    assert len(sent) == 1
    assert buttons(sent[0]) == [f"bk:wait:{booking.id}", f"bk:resched:{booking.id}", f"bk:refund:{booking.id}"]
    titles = [b["reply"]["title"] for b in sent[0]["interactive"]["action"]["buttons"]]
    assert titles == ["Keep waiting", "Pick another time", "Full refund"]
    text = body_text(sent[0])
    assert "had to cancel" in text and "fee" not in text.lower()
    assert CONDITION not in json.dumps(sent) and INSTRUCTIONS not in json.dumps(sent)


def test_the_choice_outside_the_window_is_a_template_with_three_quick_replies(db, chat):
    booking, _, _ = drop_out(db, chat, hours_ahead=1.5)
    chat.contact.last_inbound_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    sent = deliver(db, chat)
    assert sent[0]["template"]["name"] == "dropout_choice"
    quick = [c for c in sent[0]["template"]["components"] if c.get("sub_type") == "quick_reply"]
    assert [q["parameters"][0]["payload"] for q in quick] == [
        f"bk:wait:{booking.id}", f"bk:resched:{booking.id}", f"bk:refund:{booking.id}"]
    assert [q["index"] for q in quick] == ["0", "1", "2"]


# --- the decision point (worker) -------------------------------------------------------------------------------
def test_nobody_by_the_decision_lead_sends_the_choice_once(db, chat):
    booking, _, _ = drop_out(db, chat, hours_ahead=5)
    deliver(db, chat)
    start = booking.scheduled_date
    assert dropout.tick(db, start - timedelta(minutes=61)) == 0       # still time for the replacement
    assert reload(db, booking).dropout_state == "redispatching"
    assert dropout.tick(db, start - timedelta(minutes=59)) == 1
    assert dropout.tick(db, start - timedelta(minutes=30)) == 0       # not twice
    b = reload(db, booking)
    assert b.dropout_state == "awaiting_choice"
    assert len([e for e in event_names(db, b) if e == "dropout_choice_needed"]) == 1
    assert len(buttons(deliver(db, chat)[0])) == 3


def test_a_replacement_found_in_time_means_no_choice(db, chat):
    booking, _, others = drop_out(db, chat, hours_ahead=5)
    act(db, booking, others[0], "accept")
    assert dropout.tick(db, booking.scheduled_date - timedelta(minutes=30)) == 0
    assert "dropout_choice_needed" not in event_names(db, booking)


def test_the_decision_lead_is_read_from_the_policy(db, chat):
    set_policy(db, dropout_decision_lead_minutes=90)
    booking, _, _ = drop_out(db, chat, hours_ahead=5)
    assert dropout.tick(db, booking.scheduled_date - timedelta(minutes=89)) == 1


# --- the three taps ------------------------------------------------------------------------------------------------
def awaiting_choice(db, chat, hours_ahead=1.5):
    booking, caregiver, others = drop_out(db, chat, hours_ahead=hours_ahead)
    deliver(db, chat)
    return booking, caregiver, others


def test_full_refund_cancels_and_queues_the_whole_amount_with_no_fee(db, chat):
    booking, _, _ = awaiting_choice(db, chat)
    sent = chat.tap(f"bk:refund:{booking.id}")
    assert "₦44,000" in body_text(sent[0])
    b = reload(db, booking)
    assert b.status == BookingStatus.CANCELLED and b.refund_required and b.refund_required_reason == "caregiver_cancelled"
    assert b.refund_amount_kobo == b.total_amount_kobo and b.dropout_state == "closed"
    assert "fee" not in body_text(sent[0]).lower()


def test_a_second_refund_tap_changes_nothing(db, chat):
    booking, _, _ = awaiting_choice(db, chat)
    chat.tap(f"bk:refund:{booking.id}")
    again = chat.tap(f"bk:refund:{booking.id}")
    assert "already" in body_text(again[0]).lower()
    assert reload(db, booking).refund_amount_kobo == 4_400_000


def test_after_a_refund_nobody_can_accept_the_booking(db, chat):
    booking, _, others = awaiting_choice(db, chat)
    chat.tap(f"bk:refund:{booking.id}")
    from app.services.booking import BookingStateConflict
    with pytest.raises(BookingStateConflict):
        act(db, booking, others[0], "accept")


def test_keep_waiting_keeps_dispatching_and_hands_over_at_the_start_time(db, chat):
    booking, _, _ = awaiting_choice(db, chat)
    sent = chat.tap(f"bk:wait:{booking.id}")
    assert "keep looking" in body_text(sent[0]).lower()
    b = reload(db, booking)
    assert b.dropout_state == "waiting" and b.dispatch_state == "dispatching" and b.status == BookingStatus.PENDING
    assert dropout.tick(db, b.scheduled_date - timedelta(minutes=1)) == 0
    assert open_tickets(db) == []
    assert dropout.tick(db, b.scheduled_date + timedelta(minutes=1)) == 1
    b = reload(db, booking)
    assert b.dropout_state == "handed_over" and b.dispatch_state == "unmatched" and not b.refund_required
    assert [t.reason for t in open_tickets(db)] == ["talk_to_person"]
    assert chat.state == "human"
    assert "dropout_handed_over" in event_names(db, b)
    assert "our team" in body_text(deliver(db, chat)[0]).lower()
    assert dropout.tick(db, b.scheduled_date + timedelta(minutes=5)) == 0  # once


def test_keep_waiting_means_dispatch_is_not_escalated_before_the_start(db, chat):
    from app.services.dispatch import dispatch_tick
    booking, _, _ = awaiting_choice(db, chat)
    chat.tap(f"bk:wait:{booking.id}")
    started = reload(db, booking).dispatch_started_at
    dispatch_tick(db, started + timedelta(hours=3))  # far past DISPATCH_ESCALATE_MINUTES
    assert reload(db, booking).dispatch_state == "dispatching"


def test_an_automatic_replacement_keeps_looking_past_the_usual_30_minutes_until_the_decision_lead(db, chat):
    from app.services.dispatch import dispatch_tick
    booking, _, _ = drop_out(db, chat, hours_ahead=5)
    started = reload(db, booking).dispatch_started_at
    dispatch_tick(db, started + timedelta(minutes=90))  # well past DISPATCH_ESCALATE_MINUTES, 3.5 h before the start
    assert reload(db, booking).dispatch_state == "dispatching"
    dropout.tick(db, booking.scheduled_date - timedelta(minutes=59))
    assert reload(db, booking).dispatch_started_at == booking.scheduled_date - timedelta(minutes=59)


def test_pick_another_time_hands_over_with_a_ticket_and_does_not_refund(db, chat):
    booking, _, _ = awaiting_choice(db, chat)
    sent = chat.tap(f"bk:resched:{booking.id}")
    assert "team" in body_text(sent[0]).lower()
    b = reload(db, booking)
    assert b.dropout_state == "handed_over" and b.status == BookingStatus.PENDING and not b.refund_required
    assert [t.reason for t in open_tickets(db)] == ["talk_to_person"] and chat.state == "human"


def open_offers(db, booking):
    return db.query(BookingNotification).filter(BookingNotification.booking_id == booking.id,
                                                BookingNotification.response.is_(None)).count()


def test_pick_another_time_closes_the_open_offers(db, chat):
    booking, _, _ = awaiting_choice(db, chat)
    assert open_offers(db, booking) > 0  # the replacement wave is still out
    chat.tap(f"bk:resched:{booking.id}")
    assert open_offers(db, booking) == 0


def test_handing_over_at_the_start_time_closes_the_open_offers(db, chat):
    booking, _, _ = awaiting_choice(db, chat)
    assert open_offers(db, booking) > 0
    dropout.tick(db, booking.scheduled_date + timedelta(minutes=5))
    assert reload(db, booking).dropout_state == "handed_over" and open_offers(db, booking) == 0


def test_an_unanswered_choice_is_handed_over_at_the_start_time(db, chat):
    booking, _, _ = awaiting_choice(db, chat)
    assert dropout.tick(db, booking.scheduled_date + timedelta(minutes=1)) == 1
    assert reload(db, booking).dropout_state == "handed_over" and len(open_tickets(db)) == 1


@pytest.mark.parametrize("action", ["wait", "resched", "refund"])
def test_a_tap_on_a_booking_that_is_not_waiting_for_a_choice_changes_nothing(db, chat, action):
    booking, _ = accepted_booking(db, chat, hours_ahead=30)
    chat.tap(f"bk:{action}:{booking.id}")
    b = reload(db, booking)
    assert b.status == BookingStatus.ACCEPTED and not b.refund_required and b.dropout_state is None
    assert open_tickets(db) == []


@pytest.mark.parametrize("action", ["wait", "resched", "refund"])
def test_a_stranger_or_forged_id_cannot_act_on_someone_elses_choice(db, chat, action):
    stranger = make_booking(db, make_patient(db), booking_code="STRANGER1", dropout_state="awaiting_choice")
    for ident in (stranger.id, "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        sent = chat.tap(f"bk:{action}:{ident}")
        assert "STRANGER1" not in json.dumps(sent)
    b = reload(db, stranger)
    assert b.dropout_state == "awaiting_choice" and b.status == BookingStatus.PENDING and not b.refund_required


@pytest.mark.parametrize("action", ["wait", "resched", "refund"])
def test_someone_who_only_cares_for_the_recipient_cannot_decide(db, chat, action):
    from app.tests.whatsapp_notify_helpers import contact_patient  # noqa: F401
    from app.db.models.user import User  # noqa: F401
    from app.tests.factories import make_user
    other = make_user(db, "patient")
    recipient = make_recipient(db, other.id)
    booking = wa_booking(db, chat, recipient=recipient, booked_by_user_id=other.id, dropout_state="awaiting_choice")
    from app.db.models.care import CareRelationship
    db.add(CareRelationship(contact_user_id=chat.contact.user_id, recipient_id=recipient.id, role="family",
                            clinical_access=False))
    db.commit()
    chat.tap(f"bk:{action}:{booking.id}")
    b = reload(db, booking)
    assert b.dropout_state == "awaiting_choice" and not b.refund_required and open_tickets(db) == []


def test_refund_versus_a_caregiver_accepting_at_the_same_moment_gives_one_consistent_result(db, chat):
    booking, _, others = awaiting_choice(db, chat)
    contact_user_id = chat.contact.user_id
    results = {}
    barrier = threading.Barrier(2)

    def refund():
        s = SessionLocal()
        try:
            barrier.wait()
            results["refund"] = dropout.choose(s, booking.id, contact_user_id, "refund", datetime.utcnow()).status
        finally:
            s.close()

    def accept():
        s = SessionLocal()
        try:
            barrier.wait()
            BookingService(s).handle_booking_action(booking.id, others[0].id, BookingActionRequest(action="accept"))
            results["accept"] = "accepted"
        except Exception as exc:  # the loser is refused with a conflict
            results["accept"] = type(exc).__name__
        finally:
            s.close()

    threads = [threading.Thread(target=refund), threading.Thread(target=accept)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    b = reload(db, booking)
    if b.status == BookingStatus.CANCELLED:
        assert b.refund_required and b.caregiver_id is None and results["accept"] != "accepted"
    else:
        assert b.status == BookingStatus.ACCEPTED and not b.refund_required and results["accept"] == "accepted"
        assert results["refund"] in ("stale", "not_waiting")


# --- admin: the unmatched queue still works for these bookings -----------------------------------------------------
def test_a_handed_over_booking_shows_in_the_unmatched_queue_and_an_admin_can_assign_it(client, db, chat):
    from app.tests.factories import auth_headers, make_admin
    booking, _, others = awaiting_choice(db, chat)
    chat.tap(f"bk:resched:{booking.id}")
    h = auth_headers(make_admin(db, ["super_admin"], password=None))
    queue = client.get("/api/v1/admin/bookings/unmatched", headers=h).json()
    assert [i["id"] for i in queue["items"]] == [str(booking.id)] and queue["items"][0]["queue_reason"] == "unmatched"
    r = client.post(f"/api/v1/admin/bookings/{booking.id}/assign",
                    json={"reason": "family asked for the team", "caregiver_id": str(others[0].id), "force": True}, headers=h)
    assert r.status_code == 200, r.text
    b = reload(db, booking)
    assert b.caregiver_id == others[0].id and b.dropout_state is None


def test_a_second_dropout_on_the_same_booking_is_a_new_event_per_dropout(db, chat):
    booking, first, others = drop_out(db, chat, hours_ahead=5)
    deliver(db, chat)
    act(db, booking, others[0], "accept")
    act(db, booking, others[0], "cancel")
    b = reload(db, booking)
    assert b.dropout_state == "redispatching" and dropout.dropout_count(db, others[0].id) == 1
    assert len([e for e in event_names(db, b) if e == "caregiver_dropped_replacing"]) == 2


def test_the_notice_still_reaches_someone_who_sent_stop(db, chat):
    booking, _, _ = drop_out(db, chat, hours_ahead=5)
    chat.say("STOP")
    assert len(deliver(db, chat)) == 1  # essential: it is about a paid visit


# --- the service is safe on its own, not only through the chat's checks --------------------------------------------
def test_choose_refuses_anyone_but_the_booker_even_when_called_directly(db, chat):
    from app.tests.factories import make_user
    booking, _, _ = awaiting_choice(db, chat)
    stranger = make_user(db)
    outcome = dropout.choose(db, booking.id, stranger.id, "refund")
    assert outcome.status == "not_booker"
    b = reload(db, booking)
    assert b.status == BookingStatus.PENDING and not b.refund_required and b.dropout_state == "awaiting_choice"


@pytest.mark.parametrize("first, second", [("wait", "refund"), ("wait", "resched"), ("resched", "refund"),
                                           ("refund", "wait"), ("resched", "wait")])
def test_only_the_first_choice_counts_when_called_directly(db, chat, first, second):
    booking, _, _ = awaiting_choice(db, chat)
    user_id = booking.booked_by_user_id
    assert dropout.choose(db, booking.id, user_id, first).status == "done"
    assert dropout.choose(db, booking.id, user_id, second).status == "stale"
    b = reload(db, booking)
    assert b.refund_required == (first == "refund")
