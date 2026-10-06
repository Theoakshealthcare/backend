"""W1: the conversation engine is a pure function, so it is tested as a transition table.

No database, no network: ``step(snapshot, inbound, config) -> outcome``.
"""

import pytest

from app.channels.whatsapp.engine import (
    EngineConfig, Inbound, OpenTicket, RecordConsent, SetOptOut, Snapshot, State, step,
)
from app.channels.whatsapp.lexicon import is_clinical_advice_request, is_emergency
from app.channels.whatsapp.messages import ButtonsMessage, ListMessage, TextMessage

CFG = EngineConfig(emergency_numbers=("112", "767"), support_phone="", privacy_url="https://example.test/privacy",
                   policy_version="2026-10-01")

NEW, PENDING, DECLINED, IDLE, HUMAN, EMERGENCY = (State.NEW, State.CONSENT_PENDING, State.DECLINED,
                                                  State.IDLE, State.HUMAN, State.EMERGENCY)


def snap(state, *, consent_ok=False, opted_out=False, **context):
    return Snapshot(state=state, context=dict(context), consent_ok=consent_ok, opted_out=opted_out)


def kinds(outcome):
    return [{TextMessage: "text", ButtonsMessage: "buttons", ListMessage: "list"}[type(m)]
            for m in outcome.replies]


def reply_ids(outcome):
    ids = []
    for m in outcome.replies:
        if isinstance(m, ButtonsMessage):
            ids += [b[0] for b in m.buttons]
        if isinstance(m, ListMessage):
            ids += [row[0] for section in m.sections for row in section.rows]
    return ids


# (name, snapshot, inbound, expected state, expected reply kinds)
TRANSITIONS = [
    ("first contact greets and asks for consent", snap(NEW), Inbound.text("Hi"), PENDING, ["buttons"]),
    ("first contact, any text", snap(NEW), Inbound.text("I need a nurse"), PENDING, ["buttons"]),
    ("first contact, a button we never sent", snap(NEW), Inbound.button("menu:book"), PENDING, ["buttons"]),
    ("consent accepted", snap(PENDING), Inbound.button("consent:accept"), IDLE, ["list"]),
    ("consent declined", snap(PENDING), Inbound.button("consent:decline"), DECLINED, ["buttons"]),
    ("free text while consent is pending re-asks", snap(PENDING), Inbound.text("hello?"), PENDING, ["buttons"]),
    ("declined, writes again: same notice, no new prompt", snap(DECLINED), Inbound.text("hello"), DECLINED, ["buttons"]),
    ("declined, asks to start over", snap(DECLINED), Inbound.text("menu"), PENDING, ["buttons"]),
    ("declined, review again button", snap(DECLINED), Inbound.button("consent:review"), PENDING, ["buttons"]),
    ("declined, talk to a person", snap(DECLINED), Inbound.button("declined:human"), HUMAN, ["text"]),
    ("hi shows the menu", snap(IDLE, consent_ok=True), Inbound.text("Hi"), IDLE, ["list"]),
    ("menu keyword is case and punctuation tolerant", snap(IDLE, consent_ok=True), Inbound.text("  MENU! "), IDLE, ["list"]),
    ("book a visit (list)", snap(IDLE, consent_ok=True), Inbound.list_choice("menu:book"), HUMAN, ["text"]),
    ("my bookings (list)", snap(IDLE, consent_ok=True), Inbound.list_choice("menu:bookings"), HUMAN, ["text"]),
    ("doctor home visit (list)", snap(IDLE, consent_ok=True), Inbound.list_choice("menu:doctor"), HUMAN, ["text"]),
    ("talk to a person (list)", snap(IDLE, consent_ok=True), Inbound.list_choice("menu:human"), HUMAN, ["text"]),
    ("help (list) stays idle", snap(IDLE, consent_ok=True), Inbound.list_choice("menu:help"), IDLE, ["list"]),
    ("booking keyword", snap(IDLE, consent_ok=True), Inbound.text("I want to book"), HUMAN, ["text"]),
    ("price keyword", snap(IDLE, consent_ok=True), Inbound.text("price"), HUMAN, ["text"]),
    ("cancel keyword", snap(IDLE, consent_ok=True), Inbound.text("cancel"), HUMAN, ["text"]),
    ("agent keyword", snap(IDLE, consent_ok=True), Inbound.text("agent"), HUMAN, ["text"]),
    ("talk to a person keyword", snap(IDLE, consent_ok=True), Inbound.text("talk to a person"), HUMAN, ["text"]),
    ("help keyword", snap(IDLE, consent_ok=True), Inbound.text("help"), IDLE, ["list"]),
    ("unknown text: first miss", snap(IDLE, consent_ok=True), Inbound.text("asdf qwer"), IDLE, ["list"]),
    ("a keyword inside a longer sentence is not a keyword", snap(IDLE, consent_ok=True),
     Inbound.text("this is a history question"), IDLE, ["list"]),
    ("location outside a flow is a miss", snap(IDLE, consent_ok=True), Inbound.location(), IDLE, ["list"]),
    ("media is a miss", snap(IDLE, consent_ok=True), Inbound.media(), IDLE, ["list"]),
    ("unknown button id is a miss", snap(IDLE, consent_ok=True), Inbound.button("nope:nope"), IDLE, ["list"]),
    ("second consecutive miss hands over", snap(IDLE, consent_ok=True, misses=1), Inbound.text("zzz"), HUMAN, ["text"]),
    ("a human owns the conversation: silence", snap(HUMAN, consent_ok=True), Inbound.text("hello?"), HUMAN, []),
    ("emergency pause: silence", snap(EMERGENCY, consent_ok=True), Inbound.text("hello?"), EMERGENCY, []),
    ("a reaction is ignored", snap(IDLE, consent_ok=True), Inbound.ignored(), IDLE, []),
    ("opted out: only the hint", snap(IDLE, consent_ok=True, opted_out=True), Inbound.text("hello"), IDLE, ["text"]),
    ("clinical advice gets the fixed reply", snap(IDLE, consent_ok=True),
     Inbound.text("what should I take for headache"), IDLE, ["text"]),
]


@pytest.mark.parametrize("name,snapshot,inbound,state,expected_kinds", TRANSITIONS,
                         ids=[t[0] for t in TRANSITIONS])
def test_transition_table(name, snapshot, inbound, state, expected_kinds):
    outcome = step(snapshot, inbound, CFG)
    assert outcome.state == state
    assert kinds(outcome) == expected_kinds


def test_consent_screen_offers_accept_and_decline_buttons_and_the_policy_link():
    outcome = step(snap(NEW), Inbound.text("hi"), CFG)
    assert reply_ids(outcome) == ["consent:accept", "consent:decline"]
    assert "https://example.test/privacy" in outcome.replies[0].body


def test_accepting_consent_records_terms_and_health_processing_only():
    outcome = step(snap(PENDING), Inbound.button("consent:accept"), CFG)
    consent = [e for e in outcome.effects if isinstance(e, RecordConsent)]
    assert len(consent) == 1
    assert consent[0].decision == "granted"
    assert set(consent[0].types) == {"terms", "health_processing"}  # marketing is optional, never implied


def test_declining_records_no_consent_and_offers_a_human_and_a_second_look():
    outcome = step(snap(PENDING), Inbound.button("consent:decline"), CFG)
    assert not [e for e in outcome.effects if isinstance(e, RecordConsent)]
    assert reply_ids(outcome) == ["declined:human", "consent:review"]


def test_the_menu_lists_the_five_options():
    outcome = step(snap(IDLE, consent_ok=True), Inbound.text("menu"), CFG)
    assert reply_ids(outcome) == ["menu:book", "menu:bookings", "menu:doctor", "menu:human", "menu:help"]
    titles = [row[1] for s in outcome.replies[0].sections for row in s.rows]
    assert titles == ["Book a visit", "My bookings", "Doctor home visit", "Talk to a person", "Help"]


def test_booking_placeholder_is_a_handover_not_a_dead_end():
    outcome = step(snap(IDLE, consent_ok=True), Inbound.list_choice("menu:book"), CFG)
    tickets = [e for e in outcome.effects if isinstance(e, OpenTicket)]
    assert [(t.reason, t.priority) for t in tickets] == [("booking_placeholder", "normal")]
    assert "coming soon" in outcome.replies[0].body.lower()


@pytest.mark.parametrize("text", ["talk to a person", "agent", "human", "person"])
def test_talk_to_a_person_opens_a_normal_ticket(text):
    outcome = step(snap(IDLE, consent_ok=True), Inbound.text(text), CFG)
    assert [(t.reason, t.priority) for t in outcome.effects if isinstance(t, OpenTicket)] == [
        ("talk_to_person", "normal")]


def test_first_miss_counts_and_a_recognised_input_resets_the_counter():
    first = step(snap(IDLE, consent_ok=True), Inbound.text("blah"), CFG)
    assert first.context["misses"] == 1
    assert not [e for e in first.effects if isinstance(e, OpenTicket)]
    reset = step(snap(IDLE, consent_ok=True, misses=1), Inbound.text("menu"), CFG)
    assert reset.context.get("misses", 0) == 0


def test_two_misses_open_a_two_misses_ticket_and_hand_over():
    outcome = step(snap(IDLE, consent_ok=True, misses=1), Inbound.text("blah"), CFG)
    assert outcome.state == HUMAN
    assert [(t.reason, t.priority) for t in outcome.effects if isinstance(t, OpenTicket)] == [
        ("two_misses", "normal")]


# --- emergency -------------------------------------------------------------------------------
@pytest.mark.parametrize("state", list(State))
def test_emergency_screen_runs_in_every_state(state):
    outcome = step(snap(state, consent_ok=True), Inbound.text("my mother has chest pain"), CFG)
    assert outcome.state == EMERGENCY
    tickets = [e for e in outcome.effects if isinstance(e, OpenTicket)]
    assert [(t.reason, t.priority) for t in tickets] == [("emergency", "urgent")]
    assert kinds(outcome) == ["text"]
    body = outcome.replies[0].body
    assert "112" in body and "767" in body
    assert "chest" not in body.lower() and "mother" not in body.lower()  # nothing the user said is echoed


def test_emergency_beats_opt_out_and_a_withdrawn_consent():
    outcome = step(snap(DECLINED, opted_out=True), Inbound.text("he is not breathing"), CFG)
    assert outcome.state == EMERGENCY and kinds(outcome) == ["text"]


def test_emergency_screens_text_only_not_button_titles():
    outcome = step(snap(IDLE, consent_ok=True), Inbound.list_choice("menu:help"), CFG)
    assert outcome.state == IDLE


EMERGENCY_TEXTS = [
    # English
    "chest pain", "I have terrible chest pains", "pain in my chest", "I can't breathe", "cant breathe",
    "she cannot breathe", "he is struggling to breathe", "difficulty breathing", "not breathing",
    "he stopped breathing", "my dad is unconscious", "she is unresponsive", "he collapsed", "she fainted",
    "having a seizure", "he is convulsing", "heavy bleeding", "she is bleeding heavily",
    "the bleeding won't stop", "I think it's a stroke", "heart attack", "I want to kill myself",
    "thinking about suicide", "I want to die", "took an overdose", "swallowed poison", "he is choking",
    "snake bite", "vomiting blood", "CHEST PAIN!!!", "can’t breathe",
    # Nigerian Pidgin
    "I no fit breathe", "e no fit breathe", "breath no dey come out", "e no dey breathe",
    "my chest dey pain me", "blood dey comot well well", "she dey bleed plenty", "e don faint",
    "he don collapse", "I wan die", "i wan kill myself", "e no dey wake up",
]


@pytest.mark.parametrize("text", EMERGENCY_TEXTS)
def test_lexicon_matches(text):
    assert is_emergency(text), text


@pytest.mark.parametrize("text", [
    "hi", "menu", "I want to book a nurse for Saturday", "wound dressing for my mum", "how much is a visit",
    "please call me", "what time will she come", "my appointment is on Friday", "price", "e don pay",
])
def test_lexicon_ignores_ordinary_messages(text):
    assert not is_emergency(text), text


@pytest.mark.parametrize("text", [
    "what should I take for headache", "which drug is good for malaria", "what dosage of paracetamol",
    "is it safe to take ibuprofen", "wetin I go take for belle pain",
])
def test_clinical_advice_requests_are_recognised(text):
    assert is_clinical_advice_request(text), text


def test_clinical_advice_reply_never_advises_and_points_to_a_clinician():
    body = step(snap(IDLE, consent_ok=True), Inbound.text("what should I take for headache"), CFG).replies[0].body
    assert "can't give medical advice" in body.lower()
    assert "112" in body


# --- STOP / STOP DATA / START ----------------------------------------------------------------
@pytest.mark.parametrize("text", ["STOP", "stop", " Stop. "])
def test_stop_opts_out_and_keeps_the_conversation_state(text):
    outcome = step(snap(IDLE, consent_ok=True), Inbound.text(text), CFG)
    assert [e.opted_out for e in outcome.effects if isinstance(e, SetOptOut)] == [True]
    assert outcome.state == IDLE and kinds(outcome) == ["text"]
    assert not [e for e in outcome.effects if isinstance(e, (RecordConsent, OpenTicket))]


@pytest.mark.parametrize("text", ["STOP DATA", "stop data", "Stop Data."])
def test_stop_data_withdraws_consent_opens_a_compliance_task_and_says_paid_bookings_continue(text):
    outcome = step(snap(IDLE, consent_ok=True), Inbound.text(text), CFG)
    assert outcome.state == DECLINED
    withdrawals = [e for e in outcome.effects if isinstance(e, RecordConsent)]
    assert len(withdrawals) == 1 and withdrawals[0].decision == "withdrawn"
    assert set(withdrawals[0].types) == {"terms", "health_processing", "marketing"}
    assert [(t.reason, t.priority) for t in outcome.effects if isinstance(t, OpenTicket)] == [
        ("data_withdrawal", "normal")]
    assert any(isinstance(e, SetOptOut) and e.opted_out for e in outcome.effects)
    assert "paid" in outcome.replies[0].body.lower()


def test_stop_data_while_a_human_owns_the_chat_keeps_it_with_the_human():
    outcome = step(snap(HUMAN, consent_ok=True), Inbound.text("STOP DATA"), CFG)
    assert outcome.state == HUMAN
    assert any(isinstance(e, RecordConsent) for e in outcome.effects)


def test_start_resubscribes_and_shows_the_menu_when_consent_stands():
    outcome = step(snap(IDLE, consent_ok=True, opted_out=True), Inbound.text("START"), CFG)
    assert [e.opted_out for e in outcome.effects if isinstance(e, SetOptOut)] == [False]
    assert outcome.state == IDLE and kinds(outcome) == ["list"]


def test_start_after_stop_data_goes_back_through_consent():
    outcome = step(snap(DECLINED, consent_ok=False, opted_out=True), Inbound.text("start"), CFG)
    assert any(isinstance(e, SetOptOut) and not e.opted_out for e in outcome.effects)
    assert outcome.state == PENDING and reply_ids(outcome) == ["consent:accept", "consent:decline"]


def test_opted_out_user_gets_a_hint_not_the_menu():
    outcome = step(snap(IDLE, consent_ok=True, opted_out=True), Inbound.text("hello"), CFG)
    assert kinds(outcome) == ["text"] and "START" in outcome.replies[0].body


def test_stop_as_the_very_first_message():
    outcome = step(snap(NEW), Inbound.text("STOP"), CFG)
    assert any(isinstance(e, SetOptOut) and e.opted_out for e in outcome.effects)


# --- copy rules ------------------------------------------------------------------------------
def test_every_reply_respects_whatsapp_length_limits():
    cases = [snap(NEW), snap(PENDING), snap(DECLINED), snap(IDLE, consent_ok=True)]
    inbounds = [Inbound.text("hi"), Inbound.text("zzz"), Inbound.text("chest pain"), Inbound.text("stop data"),
                Inbound.button("consent:accept"), Inbound.list_choice("menu:help")]
    for s in cases:
        for i in inbounds:
            for m in step(s, i, CFG).replies:
                assert len(m.body) <= (1024 if not isinstance(m, TextMessage) else 4096)
                if isinstance(m, ButtonsMessage):
                    assert 1 <= len(m.buttons) <= 3 and all(len(t) <= 20 for _, t in m.buttons)
                if isinstance(m, ListMessage):
                    assert len(m.button_label) <= 20
                    rows = [r for sec in m.sections for r in sec.rows]
                    assert len(rows) <= 10 and all(len(r[1]) <= 24 for r in rows)


def test_support_phone_is_offered_when_configured():
    cfg = EngineConfig(emergency_numbers=("112",), support_phone="+2340000000000", privacy_url="https://x.test",
                       policy_version="v1")
    body = step(snap(PENDING), Inbound.button("consent:decline"), cfg).replies[0].body
    assert "+2340000000000" in body
