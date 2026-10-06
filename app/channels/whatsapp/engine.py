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
emergency (urgent ticket open, automation paused).
Booking (W2, PRD 9.1): idle -> booking_flow -> awaiting_location -> quoted -> awaiting_payment -> idle. The engine
only decides the next state and which booking effect to run; what a booking effect does (draft, price, create the
booking, ask Paystack) needs the database and Paystack and lives in ``booking_chat.py``, which may override the
state this module proposes (a quote that expired, a payment link that could not be made).
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
    BOOKING_FLOW = "booking_flow"
    AWAITING_LOCATION = "awaiting_location"
    QUOTED = "quoted"
    AWAITING_PAYMENT = "awaiting_payment"


PAUSED_STATES = (State.HUMAN, State.EMERGENCY)


class Kind(StrEnum):
    TEXT = "text"
    BUTTON = "button"
    LIST = "list"
    LOCATION = "location"
    FLOW = "flow"          # a finished Flow (an nfm_reply carrying only our flow token)
    MEDIA = "media"
    IGNORED = "ignored"


@dataclass(frozen=True)
class Inbound:
    kind: Kind
    body: Optional[str] = None       # the user's own words; used for matching only, never stored or echoed
    reply_id: Optional[str] = None   # the id of the button or list row they tapped, or the flow token
    lat: Optional[float] = None      # a shared location pin
    lng: Optional[float] = None

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
    def location(cls, lat: Optional[float] = None, lng: Optional[float] = None) -> "Inbound":
        return cls(Kind.LOCATION, lat=lat, lng=lng)

    @classmethod
    def flow(cls, token: str) -> "Inbound":
        return cls(Kind.FLOW, reply_id=token)

    @classmethod
    def media(cls, caption: Optional[str] = None) -> "Inbound":
        return cls(Kind.MEDIA, body=caption or None)  # the caption is screened for emergencies, nothing else

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
    can_book: bool = False     # booking in chat is on and the contact has an Oakly account to book with
    expired: bool = False      # this conversation sat idle past the limit and was just reset (WA-FR-12)


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


# --- booking effects (applied by booking_chat.py) -------------------------------------------
@dataclass(frozen=True)
class StartBooking:
    pass


@dataclass(frozen=True)
class SubmitFlow:
    token: str


@dataclass(frozen=True)
class SharedLocation:
    lat: Optional[float]
    lng: Optional[float]
    skipped: bool = False


@dataclass(frozen=True)
class ResumeDraft:
    """Say again what we are waiting for (the Flow button, or the location request)."""


@dataclass(frozen=True)
class ShowQuote:
    pass


@dataclass(frozen=True)
class ConfirmQuote:
    pass


@dataclass(frozen=True)
class ChangeQuote:
    pass


@dataclass(frozen=True)
class CancelDraft:
    pass


@dataclass(frozen=True)
class CheckPayment:
    pass


@dataclass(frozen=True)
class CancelUnpaid:
    pass


@dataclass(frozen=True)
class JoinWaitlist:
    join: bool


BOOKING_EFFECTS = (StartBooking, SubmitFlow, SharedLocation, ResumeDraft, ShowQuote, ConfirmQuote, ChangeQuote,
                   CancelDraft, CheckPayment, CancelUnpaid, JoinWaitlist)


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
    if inbound.kind == Kind.BUTTON and inbound.reply_id in ("waitlist:yes", "waitlist:no"):
        return Outcome(State.IDLE, _clean(snapshot), (), (JoinWaitlist(inbound.reply_id == "waitlist:yes"),))
    intent = _menu_intent(inbound)
    if intent is None and snapshot.expired:
        return _show_menu(snapshot, "expired_body")  # a stale tap or a stray word: start fresh, count no miss
    if intent == "menu":
        return _show_menu(snapshot)
    if intent == "help":
        return _show_menu(snapshot, "help_body")
    if intent == "human":
        return _handover(snapshot, "talk_to_person", "handover_body")
    if intent in ("book", "doctor") and snapshot.can_book:
        return Outcome(State.BOOKING_FLOW, _clean(snapshot), (), (StartBooking(),))
    if intent in ("book", "bookings", "doctor"):
        return _handover(snapshot, "booking_placeholder", "booking_placeholder_body")
    return _miss(snapshot, inbound)


# --- booking states ---------------------------------------------------------------------------
def _wants_to_leave(inbound: Inbound) -> bool:
    return inbound.kind == Kind.TEXT and _has(_words(inbound), "cancel", "back", "menu", "quit", "exit")


def _leave(snapshot: Snapshot, effect: Any) -> Outcome:
    context = {k: v for k, v in _clean(snapshot).items() if k not in ("quote_id", "booking_id")}
    return Outcome(State.IDLE, context, (copy.menu(snapshot.language, "cancelled_body"),), (effect,))


def _stay(snapshot: Snapshot, state: State, effect: Any) -> Outcome:
    return Outcome(state, _clean(snapshot), (), (effect,))


def _handle_booking_flow(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    if inbound.kind == Kind.FLOW and inbound.reply_id:
        return _stay(snapshot, State.BOOKING_FLOW, SubmitFlow(inbound.reply_id))
    if _wants_to_leave(inbound):
        return _leave(snapshot, CancelDraft())
    if _menu_intent(inbound) == "human":
        return _handover(snapshot, "talk_to_person", "handover_body")
    return _stay(snapshot, State.BOOKING_FLOW, ResumeDraft())


def _handle_awaiting_location(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    if inbound.kind == Kind.LOCATION:
        return _stay(snapshot, State.QUOTED, SharedLocation(inbound.lat, inbound.lng))
    if inbound.kind == Kind.TEXT and _has(_words(inbound), "skip"):
        return _stay(snapshot, State.QUOTED, SharedLocation(None, None, skipped=True))
    if _wants_to_leave(inbound):
        return _leave(snapshot, CancelDraft())
    if _menu_intent(inbound) == "human":
        return _handover(snapshot, "talk_to_person", "handover_body")
    return _stay(snapshot, State.AWAITING_LOCATION, ResumeDraft())


def _handle_quoted(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    choice = inbound.reply_id if inbound.kind == Kind.BUTTON else None
    words = _words(inbound)
    if choice == "quote:confirm" or (inbound.kind == Kind.TEXT and _has(words, "confirm", "pay", "yes")):
        return _stay(snapshot, State.AWAITING_PAYMENT, ConfirmQuote())
    if choice == "quote:change" or (inbound.kind == Kind.TEXT and _has(words, "change", "edit")):
        return _stay(snapshot, State.BOOKING_FLOW, ChangeQuote())
    if choice == "quote:cancel" or _wants_to_leave(inbound):
        return _leave(snapshot, CancelDraft())
    if _menu_intent(inbound) == "human":
        return _handover(snapshot, "talk_to_person", "handover_body")
    return _stay(snapshot, State.QUOTED, ShowQuote())


def _handle_awaiting_payment(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    # The booking exists now and has its own lifecycle; this state only helps the person finish paying.
    if inbound.kind == Kind.TEXT and _has(_words(inbound), "cancel"):
        return _stay(snapshot, State.IDLE, CancelUnpaid())
    if _menu_intent(inbound) == "human":
        return _handover(snapshot, "talk_to_person", "handover_body")
    return _stay(snapshot, State.AWAITING_PAYMENT, CheckPayment())


_HANDLERS = {
    State.NEW: _handle_new,
    State.CONSENT_PENDING: _handle_consent_pending,
    State.DECLINED: _handle_declined,
    State.IDLE: _handle_idle,
    State.BOOKING_FLOW: _handle_booking_flow,
    State.AWAITING_LOCATION: _handle_awaiting_location,
    State.QUOTED: _handle_quoted,
    State.AWAITING_PAYMENT: _handle_awaiting_payment,
}


def step(snapshot: Snapshot, inbound: Inbound, cfg: EngineConfig) -> Outcome:
    if inbound.kind in (Kind.TEXT, Kind.MEDIA) and inbound.body and is_emergency(inbound.body):
        return _emergency(snapshot, cfg)
    if inbound.kind == Kind.TEXT and inbound.body:
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
