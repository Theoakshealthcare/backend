"""W3 on top of A2: the admin console's cancel, reassign and force-assign tell the WhatsApp booker, in the same
transaction, and a partial refund's amount lives in ``bookings.refund_amount_kobo``."""

import pytest

from app.channels.whatsapp import notifications
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus
from app.db.models.push_token import PushToken
from app.db.models.whatsapp_booking import BookingEvent
from app.tests.fakes import expo  # noqa: F401  (fixture)
from app.tests.test_admin_bookings import (  # noqa: F401
    ACTION, boss, cancel_body, candidate, fresh, ops, paid, post,
)
from app.tests.factories import make_caregiver
from app.tests.whatsapp_notify_helpers import (  # noqa: F401
    Chat, accepted_booking, body_text, events_of, fake_meta, issuance, offer, onboarded, template_params, wa_booking,
)


@pytest.fixture
def chat(db, fake_meta):
    c = Chat(db, fake_meta)
    onboarded(c)
    return c


def deliver(db, chat):
    before = len(chat.fake.bodies)
    notifications.deliver_pending(db)
    return chat.fake.bodies[before:]


def names(db, booking):
    return sorted(e for e, _ in events_of(db, booking))


# --- cancel ------------------------------------------------------------------------------------------------
def test_a_full_refund_cancel_tells_the_booker_twice_over_and_the_caregiver_once(client, db, ops, chat, expo):
    booking, caregiver = accepted_booking(db, chat)
    db.add(PushToken(user_id=caregiver.user_id, token="ExponentPushToken[adm-cg]", platform="ios"))
    db.commit()
    deliver(db, chat)  # the journey messages so far
    r = post(client, ops, booking, "cancel", cancel_body(refund="full", reason_code="safety"))
    assert r.status_code == 200 and r.json()["refund_amount_kobo"] == booking.total_amount_kobo
    b = fresh(db, booking)
    assert (b.refund_required_reason, b.refund_amount_kobo) == ("admin_cancelled_full", b.total_amount_kobo)
    assert "caregiver_booking_cancelled" not in names(db, booking)  # A2 already pushes the caregiver itself
    texts = sorted(body_text(m) for m in deliver(db, chat))
    assert texts == sorted([
        f"Update on booking {b.booking_code}: Our team has cancelled this visit. Reply here if you need help.",
        f"Your refund of ₦44,000 for booking {b.booking_code} is being processed. Reply here if you have questions."])
    assert [m["data"]["type"] for m in expo.messages] == ["job_cancelled"]  # one push, not two


def test_a_partial_refund_stores_the_amount_not_a_string_in_the_reason(client, db, ops, chat):
    booking = wa_booking(db, chat)
    r = post(client, ops, booking, "cancel", cancel_body(refund="partial", refund_amount_kobo=1_000_000))
    assert r.status_code == 200
    b = fresh(db, booking)
    assert (b.refund_required_reason, b.refund_amount_kobo) == ("admin_cancelled_partial", 1_000_000)
    texts = [body_text(m) for m in deliver(db, chat)]
    assert any("Your refund of ₦10,000" in t for t in texts)


def test_a_cancel_without_a_refund_only_says_it_is_cancelled(client, db, ops, chat):
    booking = wa_booking(db, chat)
    post(client, ops, booking, "cancel", cancel_body(refund="none"))
    assert names(db, booking) == ["booking_cancelled_by_admin"]
    assert fresh(db, booking).refund_amount_kobo is None
    texts = [body_text(m) for m in deliver(db, chat)]
    assert texts == [f"Update on booking {booking.booking_code}: Our team has cancelled this visit. "
                     "Reply here if you need help."]


def test_a_cancel_of_an_app_booking_records_no_booker_event(client, db, ops):
    booking = paid(db)  # channel app
    post(client, ops, booking, "cancel", cancel_body(refund="full"))
    assert names(db, booking) == []


def test_a_refused_cancel_records_nothing(client, db, ops, chat):
    booking = wa_booking(db, chat, status=BookingStatus.COMPLETED)
    assert post(client, ops, booking, "cancel", cancel_body()).status_code == 409
    assert names(db, booking) == []


# --- reassign and force assign ------------------------------------------------------------------------------
def test_reassign_tells_the_booker_who_is_coming_now_and_sends_the_new_code(client, db, ops, chat):
    booking, old = accepted_booking(db, chat)
    deliver(db, chat)
    new = candidate(db, 1)
    r = post(client, ops, booking, "reassign", {"caregiver_id": str(new.id), "reason": "Caregiver no-show"})
    assert r.status_code == 200, r.text
    b = fresh(db, booking)
    key = issuance(b, new)
    assert sorted((e, k) for e, k in events_of(db, booking) if k == key) == [
        ("caregiver_reassigned", key), ("visit_code_issued", key)]
    sent = deliver(db, chat)
    texts = [body_text(m) for m in sent]
    first = new.full_name.split()[0]
    update = next(t for t in texts if "has changed" in t)
    assert update.startswith(f"Update on booking {b.booking_code}: Your professional has changed. {first} will now visit on ")
    assert next(t for t in texts if "visit code" in t) == (
        f"Your visit code for booking {b.booking_code} is {decrypt(b.visit_code_enc)}. "
        "Give it to the professional when they arrive, and to no one else.")
    assert "SENTINEL" not in str(sent)


def test_a_reassign_before_the_worker_ran_drops_the_old_caregivers_messages(client, db, ops, chat):
    booking, old = accepted_booking(db, chat)  # assigned + code for the old caregiver are still pending
    new = candidate(db, 1)
    post(client, ops, booking, "reassign", {"caregiver_id": str(new.id), "reason": "x"})
    texts = [body_text(m) for m in deliver(db, chat)]
    assert not any("Ngozi will visit" in t for t in texts)  # the old one is never announced
    assert sum("visit code" in t for t in texts) == 1  # only the new code


def test_force_assign_announces_the_caregiver_and_the_code(client, db, boss, chat):
    booking = wa_booking(db, chat)
    cg = candidate(db)
    r = post(client, boss, booking, "assign", {"caregiver_id": str(cg.id), "reason": "waiting", "force": True})
    assert r.status_code == 200, r.text
    assert names(db, booking) == ["caregiver_assigned", "visit_code_issued"]
    texts = [body_text(m) for m in deliver(db, chat)]
    assert any("will visit on" in t for t in texts) and any("visit code" in t for t in texts)


def test_an_offer_without_force_announces_nothing_until_the_caregiver_accepts(client, db, ops, chat):
    booking = wa_booking(db, chat)
    cg = candidate(db)
    post(client, ops, booking, "assign", {"caregiver_id": str(cg.id), "reason": "try"})
    assert names(db, booking) == []


# --- A -> B -> A: the booker must end up with the code that works ------------------------------------------------
def test_reassigning_a_to_b_and_back_to_a_sends_the_booker_as_new_code(client, db, ops, chat):
    booking, a = accepted_booking(db, chat)
    deliver(db, chat)  # A's first assignment and code
    b = candidate(db, 1)
    assert post(client, ops, booking, "reassign", {"caregiver_id": str(b.id), "reason": "x"}).status_code == 200
    assert post(client, ops, booking, "reassign", {"caregiver_id": str(a.id), "reason": "y"}).status_code == 200
    current = decrypt(fresh(db, booking).visit_code_enc)
    texts = [body_text(m) for m in deliver(db, chat)]
    codes = [t for t in texts if "visit code" in t]
    assert codes == [f"Your visit code for booking {booking.booking_code} is {current}. "
                     "Give it to the professional when they arrive, and to no one else."]
    assert sum("has changed" in t for t in texts) == 1 and any(f"{a.full_name.split()[0]} will now visit" in t for t in texts)


def test_a_to_b_to_a_with_the_worker_running_between_each_step(client, db, ops, chat):
    booking, a = accepted_booking(db, chat)
    b = candidate(db, 1)
    deliver(db, chat)
    post(client, ops, booking, "reassign", {"caregiver_id": str(b.id), "reason": "x"})
    first = [body_text(m) for m in deliver(db, chat)]
    code_b = decrypt(fresh(db, booking).visit_code_enc)
    assert any(code_b in t for t in first)
    post(client, ops, booking, "reassign", {"caregiver_id": str(a.id), "reason": "y"})
    second = [body_text(m) for m in deliver(db, chat)]
    code_a = decrypt(fresh(db, booking).visit_code_enc)
    assert code_a != code_b
    assert any(f"is {code_a}." in t for t in second), second  # the booker is not left holding B's dead code
