"""W3: the "my bookings" half of the conversation state machine. Pure: no database."""

import pytest

from app.channels.whatsapp.engine import (
    AskCancelBooking, ConfirmCancelBooking, EngineConfig, Inbound, ListBookings, OpenTicket, ShowBooking, Snapshot,
    State, step,
)

CFG = EngineConfig(emergency_numbers=("112",), support_phone="", privacy_url="https://example.test/privacy",
                   policy_version="2026-10-01")
BID = "7a1f0c3e-2b44-4d57-9f58-0c1d2e3f4a5b"


def snap(state=State.IDLE, *, has_account=True, **kw):
    return Snapshot(state=state, context=kw.pop("context", {}), consent_ok=True, has_account=has_account, **kw)


@pytest.mark.parametrize("inbound", [Inbound.list_choice("menu:bookings"), Inbound.button("bk:list"),
                                     Inbound.text("my bookings"), Inbound.text("cancel"), Inbound.text("reschedule")])
def test_asking_for_bookings_lists_them_for_an_account_holder(inbound):
    outcome = step(snap(), inbound, CFG)
    assert outcome.state == State.IDLE and outcome.effects == (ListBookings(),) and not outcome.replies


def test_without_an_account_my_bookings_still_hands_over():
    outcome = step(snap(has_account=False), Inbound.list_choice("menu:bookings"), CFG)
    assert outcome.state == State.HUMAN and outcome.effects[0] == OpenTicket("booking_placeholder", "normal")
    assert step(snap(has_account=False), Inbound.button(f"bk:view:{BID}"), CFG).state == State.HUMAN


@pytest.mark.parametrize("reply, effect", [
    (f"bk:view:{BID}", ShowBooking(BID)), (f"bk:keep:{BID}", ShowBooking(BID)),
    (f"bk:cancel:{BID}", AskCancelBooking(BID)), (f"bk:confirm:{BID}", ConfirmCancelBooking(BID)),
])
@pytest.mark.parametrize("make", [Inbound.button, Inbound.list_choice])
def test_each_tap_carries_the_booking_it_is_about(make, reply, effect):
    outcome = step(snap(), make(reply), CFG)
    assert outcome.state == State.IDLE and outcome.effects == (effect,)


@pytest.mark.parametrize("reply", ["bk:", "bk:view", "bk:view:", "bk:explode:1", "bk"])
def test_a_malformed_tap_is_a_miss_not_an_effect(reply):
    outcome = step(snap(), Inbound.button(reply), CFG)
    assert not outcome.effects and outcome.context.get("misses") == 1


def test_booking_taps_are_ignored_in_the_middle_of_another_journey():
    outcome = step(snap(State.QUOTED), Inbound.button(f"bk:confirm:{BID}"), CFG)
    assert not [e for e in outcome.effects if isinstance(e, (ConfirmCancelBooking, ShowBooking))]


def test_emergency_words_still_win_over_everything():
    outcome = step(snap(), Inbound.text("my bookings but also chest pain"), CFG)
    assert outcome.state == State.EMERGENCY


def test_an_opted_out_contact_gets_only_the_hint():
    outcome = step(snap(opted_out=True), Inbound.button("bk:list"), CFG)
    assert not outcome.effects and len(outcome.replies) == 1
