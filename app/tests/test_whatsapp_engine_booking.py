"""W2: the booking half of the conversation state machine (PRD 9.1) as a transition table. Pure: no database."""

import pytest

from app.channels.whatsapp.engine import (
    CancelDraft, CancelUnpaid, ChangeQuote, CheckPayment, ConfirmQuote, EngineConfig, Inbound, JoinWaitlist, OpenTicket,
    ResumeDraft, SharedLocation, ShowQuote, Snapshot, StartBooking, State, SubmitFlow, step,
)
from app.channels.whatsapp.messages import ListMessage

CFG = EngineConfig(emergency_numbers=("112",), support_phone="", privacy_url="https://example.test/privacy",
                   policy_version="2026-10-01")
FLOW, LOCATION, QUOTED, PAYING, IDLE = (State.BOOKING_FLOW, State.AWAITING_LOCATION, State.QUOTED,
                                        State.AWAITING_PAYMENT, State.IDLE)


def snap(state, *, can_book=True, **kw):
    return Snapshot(state=state, context=kw.pop("context", {}), consent_ok=True, can_book=can_book, **kw)


def effects(outcome, kind):
    return [e for e in outcome.effects if isinstance(e, kind)]


# --- opening a booking -----------------------------------------------------------------------------
@pytest.mark.parametrize("inbound", [Inbound.list_choice("menu:book"), Inbound.list_choice("menu:doctor"),
                                     Inbound.button("menu:book"), Inbound.text("book"), Inbound.text("I want to book a visit"),
                                     Inbound.text("how much")])
def test_book_intents_start_the_booking_when_it_is_available(inbound):
    outcome = step(snap(IDLE), inbound, CFG)
    assert outcome.state == FLOW and effects(outcome, StartBooking) and not outcome.replies


@pytest.mark.parametrize("can_book", [False])
def test_book_still_hands_over_when_booking_in_chat_is_off_or_there_is_no_account(can_book):
    outcome = step(snap(IDLE, can_book=can_book), Inbound.list_choice("menu:book"), CFG)
    assert outcome.state == State.HUMAN and effects(outcome, OpenTicket)[0].reason == "booking_placeholder"


def test_my_bookings_is_still_a_placeholder_until_w3():
    outcome = step(snap(IDLE), Inbound.list_choice("menu:bookings"), CFG)
    assert outcome.state == State.HUMAN


def test_a_reset_conversation_shows_the_menu_for_a_stray_word_without_counting_a_miss():
    outcome = step(snap(IDLE, expired=True), Inbound.text("asdf"), CFG)
    assert outcome.state == IDLE and isinstance(outcome.replies[0], ListMessage)
    assert outcome.context.get("misses", 0) == 0 and "while" in outcome.replies[0].body
    # ...but a real intent is still honoured
    assert step(snap(IDLE, expired=True), Inbound.text("book"), CFG).state == FLOW


# --- the Flow is open ----------------------------------------------------------------------------------
def test_the_completed_flow_is_handed_to_the_booking_step():
    outcome = step(snap(FLOW), Inbound.flow("tok"), CFG)
    assert outcome.state == FLOW and effects(outcome, SubmitFlow) == [SubmitFlow("tok")]


@pytest.mark.parametrize("word", ["cancel", "back", "menu", "quit", "CANCEL", "exit"])
def test_leaving_the_flow_cancels_the_draft_and_shows_the_menu(word):
    outcome = step(snap(FLOW, context={"quote_id": "q"}), Inbound.text(word), CFG)
    assert outcome.state == IDLE and effects(outcome, CancelDraft) and isinstance(outcome.replies[0], ListMessage)
    assert "quote_id" not in outcome.context


def test_anything_else_while_the_flow_is_open_resumes_it_and_is_not_a_miss():
    outcome = step(snap(FLOW), Inbound.text("hello?"), CFG)
    assert outcome.state == FLOW and effects(outcome, ResumeDraft) and outcome.context.get("misses", 0) == 0
    assert effects(step(snap(FLOW), Inbound.media(), CFG), ResumeDraft)


def test_a_person_can_be_asked_for_at_every_booking_step():
    for state in (FLOW, LOCATION, QUOTED, PAYING):
        outcome = step(snap(state), Inbound.text("talk to a person"), CFG)
        assert outcome.state == State.HUMAN and effects(outcome, OpenTicket)[0].reason == "talk_to_person", state


# --- location ----------------------------------------------------------------------------------------------
def test_a_pin_moves_on_to_the_quote():
    outcome = step(snap(LOCATION), Inbound.location(6.45, 3.47), CFG)
    assert outcome.state == QUOTED and effects(outcome, SharedLocation) == [SharedLocation(6.45, 3.47)]


def test_skip_moves_on_without_a_pin():
    outcome = step(snap(LOCATION), Inbound.text("skip"), CFG)
    assert outcome.state == QUOTED and effects(outcome, SharedLocation) == [SharedLocation(None, None, skipped=True)]


def test_other_input_while_waiting_for_the_pin_asks_again_and_cancel_leaves():
    assert effects(step(snap(LOCATION), Inbound.text("hmm"), CFG), ResumeDraft)
    assert step(snap(LOCATION), Inbound.text("cancel"), CFG).state == IDLE


# --- the quote --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("inbound,state,effect", [
    (Inbound.button("quote:confirm"), PAYING, ConfirmQuote), (Inbound.text("confirm"), PAYING, ConfirmQuote),
    (Inbound.text("yes"), PAYING, ConfirmQuote), (Inbound.button("quote:change"), FLOW, ChangeQuote),
    (Inbound.text("change"), FLOW, ChangeQuote), (Inbound.button("quote:cancel"), IDLE, CancelDraft),
    (Inbound.text("cancel"), IDLE, CancelDraft), (Inbound.text("hmm?"), QUOTED, ShowQuote),
    (Inbound.button("something:else"), QUOTED, ShowQuote), (Inbound.media(), QUOTED, ShowQuote),
])
def test_the_quote_buttons_and_their_words(inbound, state, effect):
    outcome = step(snap(QUOTED), inbound, CFG)
    assert outcome.state == state and effects(outcome, effect)


# --- paying ------------------------------------------------------------------------------------------------
def test_anything_while_awaiting_payment_checks_payment_and_cancel_cancels_the_unpaid_booking():
    for inbound in (Inbound.text("I paid"), Inbound.button("quote:confirm"), Inbound.text("hi"), Inbound.media()):
        outcome = step(snap(PAYING, context={"booking_id": "b"}), inbound, CFG)
        assert outcome.state == PAYING and effects(outcome, CheckPayment), inbound
    outcome = step(snap(PAYING), Inbound.text("cancel"), CFG)
    assert outcome.state == IDLE and effects(outcome, CancelUnpaid)


# --- the waitlist -----------------------------------------------------------------------------------------------
def test_waitlist_buttons_are_answered_in_idle():
    assert effects(step(snap(IDLE), Inbound.button("waitlist:yes"), CFG), JoinWaitlist) == [JoinWaitlist(True)]
    assert effects(step(snap(IDLE), Inbound.button("waitlist:no"), CFG), JoinWaitlist) == [JoinWaitlist(False)]


# --- the safety rules still come first, in every booking state ---------------------------------------------------
@pytest.mark.parametrize("state", [FLOW, LOCATION, QUOTED, PAYING])
def test_the_emergency_screen_runs_first_in_every_booking_state(state):
    outcome = step(snap(state), Inbound.text("he is unconscious and not breathing"), CFG)
    assert outcome.state == State.EMERGENCY and effects(outcome, OpenTicket)[0].priority == "urgent"
    assert not effects(outcome, SubmitFlow) and not effects(outcome, ConfirmQuote)


@pytest.mark.parametrize("state", [FLOW, LOCATION, QUOTED, PAYING])
def test_stop_data_and_stop_work_in_every_booking_state(state):
    assert step(snap(state), Inbound.text("STOP DATA"), CFG).state == State.DECLINED
    stop = step(snap(state), Inbound.text("STOP"), CFG)
    assert stop.state == state  # the draft or booking is not destroyed by an unsubscribe


@pytest.mark.parametrize("state", [FLOW, LOCATION, QUOTED, PAYING])
def test_an_opted_out_contact_gets_only_the_hint_in_a_booking_state(state):
    outcome = step(snap(state, opted_out=True), Inbound.text("confirm"), CFG)
    assert not outcome.effects and "unsubscribed" in outcome.replies[0].body


def test_a_human_owned_conversation_is_silent_whatever_is_said():
    assert step(snap(State.HUMAN), Inbound.text("confirm"), CFG).effects == ()
