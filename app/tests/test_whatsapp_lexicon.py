"""Review-fix coverage for the emergency lexicon: gaps found by the security review, misspellings,
and a few must-not-trigger negatives so the net does not become noise."""

import pytest

from app.channels.whatsapp.engine import EngineConfig, Inbound, Snapshot, State, step
from app.channels.whatsapp.lexicon import is_emergency, normalize

POSITIVE = [
    # breathing
    "shortness of breath", "she has difficulty in breathing", "breathing difficulty", "he is gasping",
    "he is breathing hard", "Short of breath!!",
    # heart / chest
    "my heart is paining me", "my chest is tight", "tight chest", "chest is too tight",
    # consciousness
    "he is not waking up", "she wont wake up", "she won't wake up", "he slumped on the floor",
    "she has slumped",
    # bleeding
    "she is bleeding", "bleeding", "blood is coming out", "I am pregnant and bleeding",
    # poison / self-harm
    "my brother drank sniper", "sniper", "I will hang myself", "i no wan live again", "I want to end it all",
    "I am tired of life",
    # seizures
    "he is having fit", "the child dey fit", "foaming at the mouth", "she is foaming at her mouth",
    # death / trauma
    "my father is dead", "grandma has died", "he was stabbed", "there was an accident",
    # misspellings
    "he is unconcious", "she had a siezure", "thinking of sucide", "unconsious", "convulsions",
    # emphasis / spacing
    "  CHEST   PAIN  ", "não... can't breathe",
]

NEGATIVE = [
    "book a nurse for my mum next week", "I want to book a visit on Friday", "how do I pay",
    "wound dressing for my mum", "she dey fit pay on Friday", "reading glasses", "my appointment is on Friday",
    "booking", "please call me", "what time will she come", "hi", "menu",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_emergency_lexicon_matches(text):
    assert is_emergency(text), text


@pytest.mark.parametrize("text", NEGATIVE)
def test_emergency_lexicon_does_not_trigger_on_ordinary_messages(text):
    assert not is_emergency(text), text


def test_normalisation_folds_case_apostrophes_punctuation_and_whitespace():
    assert normalize("  Won’t   WAKE-up!! ") == "wont wake up"


CFG = EngineConfig(emergency_numbers=("112",), support_phone="", privacy_url="https://x.test", policy_version="v1")


@pytest.mark.parametrize("state", [State.NEW, State.CONSENT_PENDING, State.IDLE, State.HUMAN])
def test_a_media_caption_is_screened_like_text(state):
    outcome = step(Snapshot(state=state, consent_ok=True), Inbound.media("my mother is not breathing"), CFG)
    assert outcome.state == State.EMERGENCY


def test_a_media_caption_that_is_not_an_emergency_is_still_media():
    outcome = step(Snapshot(state=State.IDLE, consent_ok=True), Inbound.media("here is the prescription"), CFG)
    assert outcome.state != State.EMERGENCY
