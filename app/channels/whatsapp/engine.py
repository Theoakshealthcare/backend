"""The conversation engine: a pure state machine (PRD 9.1).

``step(snapshot, inbound, config)`` takes the conversation as it is and one inbound message, and
returns the next state, the next context, what to say, and the side effects to apply. It does no I/O:
no database, no HTTP, no clock. ``processor.py`` loads the snapshot, calls this, and applies the
outcome, which is what keeps the whole transition table unit-testable.

Order of checks for every inbound message (the order is the safety design):
 1. Emergency screen on any text, in any state, even when opted out or paused.
 2. STOP DATA / STOP / START, in any state.
 3. While a human owns the conversation the bot is silent (the emergency screen above still runs).
 4. Reactions and other no-ops are ignored.
 5. An opted-out contact gets one hint line, never the menu.
 6. Otherwise the state's own handler.

States: new -> consent_pending -> idle <-> human; declined (consent refused or withdrawn);
emergency (urgent ticket open, automation paused). Booking states arrive with W2.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Optional, Tuple

from app.channels.whatsapp import copy
from app.channels.whatsapp.lexicon import is_clinical_advice_request, is_emergency, normalize
from app.channels.whatsapp.messages import OutboundMessage

REQUIRED_CONSENTS = ("terms", "health_processing")
ALL_WITHDRAWABLE = ("terms", "health_processing", "marketing")
MAX_MISSES = 2
MAX_KEYWORD_WORDS = 8  # a keyword only counts in a short message, so a sentence is never half-matched


class State(StrEnum):
    NEW = "new"
    CONSENT_PENDING = "consent_pending"
    DECLINED = "declined"
    IDLE = "idle"
    HUMAN = "human"
    EMERGENCY = "emergency"


PAUSED_STATES = (State.HUMAN, State.EMERGENCY)


class Kind(StrEnum):
    TEXT = "text"
    BUTTON = "button"
    LIST = "list"
    LOCATION = "location"
    MEDIA = "media"
    IGNORED = "ignored"


@dataclass(frozen=True)
class Inbound:
    kind: Kind
    body: Optional[str] = None       # the user's own words; used for matching only, never stored or echoed
    reply_id: Optional[str] = None   # the id of the button or list row they tapped

    @classmethod
    def text(cls, body: str) -> "Inbound":
        return cls(Kind.TEXT, body=body)

    @classmethod
    def button(cls, reply_id: str) -> "Inbound":
        return cls(Kind.BUTTON, reply_id=reply_id)

    @classmethod
    def list_choice(cls, reply_id: str) -> "Inbound":
        return cls(Kind.LIST, reply_id=reply_id)

    @classmethod
    def location(cls) -> "Inbound":
        return cls(Kind.LOCATION)

    @classmethod
    def media(cls) -> "Inbound":
        return cls(Kind.MEDIA)

    @classmethod
    def ignored(cls) -> "Inbound":
        return cls(Kind.IGNORED)


@dataclass(frozen=True)
class Snapshot:
    state: State
    context: Mapping[str, Any] = field(default_factory=dict)
    consent_ok: bool = False   # the required consents currently stand
    opted_out: bool = False
    language: str = "en"


@dataclass(frozen=True)
class EngineConfig:
    emergency_numbers: Tuple[str, ...]
    support_phone: str
    privacy_url: str
    policy_version: str


@dataclass(frozen=True)
class RecordConsent:
    types: Tuple[str, ...]
    decision: str  # granted | withdrawn


@dataclass(frozen=True)
class SetOptOut:
    opted_out: bool


@dataclass(frozen=True)
class OpenTicket:
    reason: str
    priority: str  # normal | urgent


@dataclass(frozen=True)
class Outcome:
    state: State
    context: Mapping[str, Any]
    replies: Tuple[OutboundMessage, ...] = ()
    effects: Tuple[Any, ...] = ()


# --- helpers ---------------------------------------------------------------------------------
def _ctx(snapshot: Snapshot, **changes: Any) -> dict:
    return {**snapshot.context, **changes}


def _clean(snapshot: Snapshot) -> dict:
    return _ctx(snapshot, misses=0)


def _numbers(cfg: EngineConfig) -> str:
    return " or ".join(cfg.emergency_numbers)


def _words(inbound: Inbound) -> list[str]:
    return normalize(inbound.body).split() if inbound.kind == Kind.TEXT and inbound.body else []


def _has(words: list[str], *tokens: str) -> bool:
    return any(t in words for t in tokens)


def _phrase(words: list[str], phrase: str) -> bool:
    joined = " " + " ".join(words) + " "
    return f" {phrase} " in joined


# --- the intents a short message can carry --------------------------------------------------
def _keyword_intent(inbound: Inbound) -> Optional[str]:
    words = _words(inbound)
    if not words or len(words) > MAX_KEYWORD_WORDS:
        return None
    if (_has(words, "agent", "human", "person", "someone", "representative", "staff")
            or _phrase(words, "talk to") or _phrase(words, "speak to")):
        return "human"
    if _phrase(words, "my bookings") or _phrase(words, "my booking") or _has(words, "bookings"):
        return "bookings"
    if _has(words, "doctor"):
        return "doctor"
    if _has(words, "book", "booking", "appointment"):
        return "book"
    if _has(words, "cancel", "reschedule"):
        return "bookings"
    if _has(words, "price", "prices", "cost", "charges") or _phrase(words, "how much"):
        return "book"
    if _has(words, "help"):
        return "help"
    if _has(words, "menu", "hi", "hello", "hey", "hiya", "hola"):
        return "menu"
    return None


def _menu_intent(inbound: Inbound) -> Optional[str]:
    if inbound.kind in (Kind.LIST, Kind.BUTTON) and inbound.reply_id and inbound.reply_id.startswith("menu:"):
        name = inbound.reply_id.split(":", 1)[1]
        return name if name in ("book", "bookings", "doctor", "human", "help") else None
    return _keyword_intent(inbound)


# --- outcomes --------------------------------------------------------------------------------
def _handover(snapshot: Snapshot, reason: str, reply_key: str) -> Outcome:
    return Outcome(State.HUMAN, _clean(snapshot), (copy.text(snapshot.language, reply_key),),
                   (OpenTicket(reason, "normal"),))


def _show_menu(snapshot: Snapshot, body_key: str = "menu_body") -> Outcome:
    return Outcome(State.IDLE, _clean(snapshot), (copy.menu(snapshot.language, body_key),))


def _ask_consent(snapshot: Snapshot, cfg: EngineConfig) -> Outcome:
    return Outcome(State.CONSENT_PENDING, _clean(snapshot),
                   (copy.consent_prompt(snapshot.language, cfg.privacy_url),))


def _miss(snapshot: Snapshot, inbound: Inbound) -> Outcome:
    misses = int(snapshot.context.get("misses", 0)) + 1
    if misses >= MAX_MISSES:
        return _handover(snapshot, "two_misses", "two_misses_body")
    key = "miss_body" if inbound.kind in (Kind.TEXT, Kind.BUTTON, Kind.LIST) else "miss_media_body"
    return Outcome(State.IDLE, _ctx(snapshot, misses=misses), (copy.menu(snapshot.language, key),))


def _emergency(snapshot: Snapshot, cfg: EngineConfig) -> Outcome:
    return Outcome(State.EMERGENCY, _clean(snapshot),
                   (copy.text(snapshot.language, "emergency_body", numbers=_numbers(cfg)),),
                   (OpenTicket("emergency", "urgent"),))


def _stop_data(snapshot: Snapshot) -> Outcome:
    state = snapshot.state if snapshot.state in PAUSED_STATES else State.DECLINED
    return Outcome(state, _clean(snapshot), (copy.text(snapshot.language, "stop_data_body"),),
                   (RecordConsent(ALL_WITHDRAWABLE, "withdrawn"), SetOptOut(True),
                    OpenTicket("data_withdrawal", "normal")))


def _stop(snapshot: Snapshot) -> Outcome:
    return Outcome(snapshot.state, dict(snapshot.context), (copy.text(snapshot.language, "stop_body"),),
                   (SetOptOut(True),))


def _start(snapshot: Snapshot, cfg: EngineConfig) -> Outcome:
    resubscribe = (SetOptOut(False),)
    if snapshot.state in PAUSED_STATES:
        return Outcome(snapshot.state, dict(snapshot.context),
                       (copy.text(snapshot.language, "start_body"),), resubscribe)
    if not snapshot.consent_ok:
        outcome = _ask_consent(snapshot, cfg)
        return Outcome(outcome.state, outcome.context, outcome.replies, resubscribe)
    body = copy.t(snapshot.language, "start_body") + " " + copy.t(snapshot.language, "menu_body")
    return Outcome(State.IDLE, _clean(snapshot), (copy.menu_with_body(snapshot.language, body),), resubscribe)


# --- per-state handlers ----------------------------------------------------------------------
def _handle_new(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    return _ask_consent(snapshot, cfg)


def _handle_consent_pending(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    if inbound.kind == Kind.BUTTON and inbound.reply_id == "consent:accept":
        body = copy.t(snapshot.language, "consent_thanks_menu")
        return Outcome(State.IDLE, _clean(snapshot), (copy.menu_with_body(snapshot.language, body),),
                       (RecordConsent(REQUIRED_CONSENTS, "granted"),))
    if inbound.kind == Kind.BUTTON and inbound.reply_id == "consent:decline":
        return Outcome(State.DECLINED, _clean(snapshot), (copy.declined_notice(snapshot.language, cfg.support_phone),))
    return _ask_consent(snapshot, cfg)


def _handle_declined(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    if inbound.kind == Kind.BUTTON and inbound.reply_id == "declined:human":
        return _handover(snapshot, "talk_to_person", "handover_body")
    wants_review = (inbound.kind == Kind.BUTTON and inbound.reply_id == "consent:review") \
        or (inbound.kind == Kind.TEXT and _has(_words(inbound), "menu"))
    if wants_review:
        return _ask_consent(snapshot, cfg)
    # WA-FR-7: nothing beyond a reply to the message they just sent.
    return Outcome(State.DECLINED, _clean(snapshot), (copy.declined_notice(snapshot.language, cfg.support_phone),))


def _handle_idle(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    if not snapshot.consent_ok:
        return _ask_consent(snapshot, cfg)
    if inbound.kind == Kind.TEXT and inbound.body and is_clinical_advice_request(inbound.body):
        return Outcome(State.IDLE, dict(snapshot.context),
                       (copy.text(snapshot.language, "clinical_body", numbers=_numbers(cfg)),))
    intent = _menu_intent(inbound)
    if intent == "menu":
        return _show_menu(snapshot)
    if intent == "help":
        return _show_menu(snapshot, "help_body")
    if intent == "human":
        return _handover(snapshot, "talk_to_person", "handover_body")
    if intent in ("book", "bookings", "doctor"):
        return _handover(snapshot, "booking_placeholder", "booking_placeholder_body")
    return _miss(snapshot, inbound)


_HANDLERS = {
    State.NEW: _handle_new,
    State.CONSENT_PENDING: _handle_consent_pending,
    State.DECLINED: _handle_declined,
    State.IDLE: _handle_idle,
}


def step(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    if inbound.kind == Kind.TEXT and inbound.body:
        if is_emergency(inbound.body):
            return _emergency(snapshot, cfg)
        keyword = normalize(inbound.body)
        if keyword in ("stop data", "stopdata"):
            return _stop_data(snapshot)
        if keyword in ("stop", "unsubscribe"):
            return _stop(snapshot)
        if keyword == "start":
            return _start(snapshot, cfg)

    if snapshot.state in PAUSED_STATES:
        return Outcome(snapshot.state, dict(snapshot.context))
    if inbound.kind == Kind.IGNORED:
        return Outcome(snapshot.state, dict(snapshot.context))
    if snapshot.opted_out:
        return Outcome(snapshot.state, dict(snapshot.context), (copy.text(snapshot.language, "stop_hint_body"),))
    return _HANDLERS[snapshot.state](snapshot, inbound, cfg)
