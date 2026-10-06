"""What the booking effects of the conversation engine do (W2): draft, location, quote, booking, payment.

``engine.step`` decides the state and names an effect; this module does the work, against the database and the
domain services (pricing, the booking service, Paystack settlement) and returns a ``Result``: the replies to send,
optionally a state and context changes that override the engine's proposal, and optionally an ``after_commit``
step for the slow part (Paystack over HTTP) that must not run while the conversation row is locked.

Guarantees worth knowing before changing anything here
- One Confirm creates one booking. The conversation row is locked for the whole step (two workers handling the same
  contact queue up), the quote row is locked, and ``quotes.booking_id`` is unique. A second tap finds the booking
  and resends the pay link instead of creating anything.
- A quote's price is read from the quote, never recomputed: the booking is created with exactly the kobo that was
  shown and locked (WA-FR-20), whatever the price settings say by then.
- Nothing the person typed in the Flow (condition, instructions) is in any reply, log line or context. It sits
  encrypted on the quote until the booking exists, then encrypted on the booking (the condition) or in its
  special instructions (the app's own convention); the quote's copy is wiped once the payment link exists.
- Failures leave the person somewhere sensible: no payment link means back to the quote with a "try again", never a
  booking nobody can pay.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_rules as rules
from app.channels.whatsapp import copy
from app.channels.whatsapp.engine import (
    CancelDraft, CancelUnpaid, ChangeQuote, CheckPayment, ConfirmQuote, JoinWaitlist, ResumeDraft, SharedLocation,
    PAUSED_STATES, ShowQuote, StartBooking, State, SubmitFlow,
)
from app.channels.whatsapp.flows import token as flow_token
from app.channels.whatsapp.messages import (
    ButtonsMessage, CtaUrlMessage, FlowMessage, LocationRequestMessage, OutboundMessage, TextMessage,
)
from app.core.config import settings
from app.core.security import decrypt, encrypt
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.patient import Patient
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation
from app.db.models.whatsapp_booking import OPEN_QUOTE_STATUSES, Quote
from app.schemas.booking import BookingCreate
from app.services.booking import BookingService
from app.services.geocoding import build_geocoder
from app.services.pricing import PriceBreakdown, price_service
from app.services.support import SupportService

logger = logging.getLogger(__name__)

# What stays on a quote once nobody is going to finish it: enough to explain a price or a waitlist, no address.
KEEP_WHEN_CLOSED = ("service", "city", "duration_hours", "date", "time", "breakdown", "reason")
PAYMENT_INIT_GRACE = timedelta(seconds=120)  # a booking with no payment link older than this lost its init


@dataclass
class Result:
    replies: List[OutboundMessage] = field(default_factory=list)
    state: Optional[State] = None                       # overrides the engine's proposed state
    context: Dict[str, Any] = field(default_factory=dict)  # merged into the conversation context; None deletes
    after_commit: Optional[Callable[[Session], "Result"]] = None


@dataclass
class Ctx:
    db: Session
    contact: WhatsAppContact
    conversation: WhatsAppConversation
    now: datetime

    @property
    def lang(self) -> str:
        return self.contact.language

    @property
    def quote_id(self) -> Optional[uuid.UUID]:
        return _uuid(self.conversation.context.get("quote_id") if self.conversation.context else None)


def _uuid(raw: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


def _text(ctx: Ctx, key: str, **values: str) -> TextMessage:
    return copy.text(ctx.lang, key, **values)


def _menu(ctx: Ctx, key: str) -> OutboundMessage:
    return copy.menu(ctx.lang, key)


def _to_idle(*replies: OutboundMessage) -> Result:
    return Result(list(replies), State.IDLE, {"quote_id": None, "booking_id": None, "waitlist_quote_id": None})


# --- quotes: lookup and closing ------------------------------------------------------------------
def _lock_quote(ctx: Ctx, quote_id: Optional[uuid.UUID]) -> Optional[Quote]:
    if quote_id is None:
        return None
    quote = (ctx.db.query(Quote).filter(Quote.id == quote_id, Quote.contact_id == ctx.contact.id)
             .populate_existing().with_for_update().first())
    return quote


def close_quote(quote: Quote, status: str) -> None:
    """End a draft or quote: never again used, and no sensitive text and no address left on it."""
    quote.status = status
    quote.sensitive_encrypted = None
    quote.payload = {k: v for k, v in (quote.payload or {}).items() if k in KEEP_WHEN_CLOSED}


def close_open_quotes(db: Session, contact_id: uuid.UUID, status: str = "cancelled") -> int:
    open_quotes = (db.query(Quote).filter(Quote.contact_id == contact_id, Quote.status.in_(OPEN_QUOTE_STATUSES))
                   .populate_existing().with_for_update().all())
    for quote in open_quotes:
        close_quote(quote, status)
    return len(open_quotes)


# --- starting ------------------------------------------------------------------------------------------
def _flow_message(ctx: Ctx, quote: Quote, body_key: str) -> FlowMessage:
    return FlowMessage(
        body=copy.t(ctx.lang, body_key), flow_id=settings.WHATSAPP_BOOKING_FLOW_ID,
        flow_token=flow_token.issue(quote.id, ctx.now), cta=copy.t(ctx.lang, "flow_cta"), action="data_exchange",
    )


def start_booking(ctx: Ctx) -> Result:
    close_open_quotes(ctx.db, ctx.contact.id)  # one open booking attempt per person
    quote = Quote(contact_id=ctx.contact.id, status="draft", payload={})
    ctx.db.add(quote)
    ctx.db.flush()
    return Result([_flow_message(ctx, quote, "flow_body")], State.BOOKING_FLOW, {"quote_id": str(quote.id)})


def resume_draft(ctx: Ctx) -> Result:
    state = State(ctx.conversation.state)
    quote = _lock_quote(ctx, ctx.quote_id)
    if state == State.BOOKING_FLOW and quote is not None and quote.status == "draft":
        return Result([_flow_message(ctx, quote, "flow_resume_body")], State.BOOKING_FLOW)
    if state == State.AWAITING_LOCATION and quote is not None and quote.status == "submitted":
        return Result([LocationRequestMessage(copy.t(ctx.lang, "location_resume_body"))], State.AWAITING_LOCATION)
    return _stale(ctx)


def _stale(ctx: Ctx) -> Result:
    return _to_idle(_menu(ctx, "flow_stale_body"))


def cancel_draft(ctx: Ctx) -> Result:
    quote = _lock_quote(ctx, ctx.quote_id)
    if quote is not None and quote.status in OPEN_QUOTE_STATUSES:
        close_quote(quote, "cancelled")
    return Result(context={"quote_id": None, "booking_id": None})  # the engine already said "cancelled"


# --- the Flow was submitted --------------------------------------------------------------------------
def submit_flow(ctx: Ctx, effect: SubmitFlow) -> Result:
    quote_id = flow_token.verify(effect.token, ctx.now)
    quote = _lock_quote(ctx, quote_id)
    if quote is None:
        return _stale(ctx)
    if quote.status == "submitted":
        if (quote.payload or {}).get("emergency_flag"):
            # Words in the Flow matched the emergency screen: a person looks at this now, whatever the booking does.
            logger.error("whatsapp emergency words inside the booking Flow for contact; urgent ticket opened")
            SupportService(ctx.db).open(contact_id=ctx.contact.id, reason="emergency", priority="urgent")
        quote_context = {"quote_id": str(quote.id)}
        return Result([LocationRequestMessage(copy.t(ctx.lang, "location_body"))], State.AWAITING_LOCATION,
                      quote_context)
    if quote.status == "out_of_area":
        city = (quote.payload or {}).get("city")
        body = (copy.t(ctx.lang, "waitlist_offer_body", city=city) if city
                else copy.t(ctx.lang, "waitlist_offer_body_unnamed"))
        offer = ButtonsMessage(body, (("waitlist:yes", copy.t(ctx.lang, "waitlist_yes")),
                                      ("waitlist:no", copy.t(ctx.lang, "waitlist_no"))))
        return Result([offer], State.IDLE, {"quote_id": None, "waitlist_quote_id": str(quote.id)})
    if quote.status == "cancelled" and (quote.payload or {}).get("reason") == "no_services":
        SupportService(ctx.db).open(contact_id=ctx.contact.id, reason="booking_placeholder", priority="normal")
        return Result([_text(ctx, "no_services_body")], State.HUMAN, {"quote_id": None})
    if quote.status == "draft":
        return Result([_flow_message(ctx, quote, "flow_resume_body")], State.BOOKING_FLOW)
    return _stale(ctx)


def join_waitlist(ctx: Ctx, effect: JoinWaitlist) -> Result:
    quote = _lock_quote(ctx, _uuid((ctx.conversation.context or {}).get("waitlist_quote_id")))
    if quote is not None and quote.status == "out_of_area":
        close_quote(quote, "waitlist" if effect.join else "cancelled")
    key = "waitlist_thanks_body" if effect.join else "waitlist_declined_body"
    reply = _text(ctx, key) if effect.join else _menu(ctx, key)
    return Result([reply], State.IDLE, {"waitlist_quote_id": None})


# --- location and the quote ----------------------------------------------------------------------
def shared_location(ctx: Ctx, effect: SharedLocation) -> Result:
    quote = _lock_quote(ctx, ctx.quote_id)
    if quote is None or quote.status != "submitted":
        return _stale(ctx)
    payload = dict(quote.payload or {})
    if not effect.skipped and rules.pin_is_plausible(effect.lat, effect.lng):
        payload.update(lat=float(effect.lat), lng=float(effect.lng), needs_address_check=False, location_source="pin")
        quote.payload = payload
        return _price(ctx, quote)
    # No usable pin (skipped, or somewhere that is not Nigeria). Try the typed address first; that is an HTTP call,
    # so it runs after this step commits, with no row locked (``_geocode_then_price``). With no geocoder
    # configured, or no address to look up, it is the placeholder straight away.
    if build_geocoder().enabled and payload.get("address") and payload.get("city"):
        contact_id, quote_id = ctx.contact.id, quote.id
        return Result(state=State.AWAITING_LOCATION,  # stay put until the lookup has answered
                      after_commit=lambda db: _geocode_then_price(db, contact_id, quote_id))
    _use_placeholder(payload)
    quote.payload = payload
    return _price(ctx, quote)


def _use_placeholder(payload: Dict[str, Any]) -> None:
    """The city centre as a stand-in, and the booking held for a person to confirm the address before any
    caregiver is offered it (WA-FR-14)."""
    lat, lng = rules.CITY_CENTRE.get(payload.get("city", ""), rules.CITY_CENTRE["Lagos"])
    payload.update(lat=lat, lng=lng, needs_address_check=True, location_source="placeholder")


GEOCODE_IN_FLIGHT_TTL = timedelta(seconds=15)  # longer than the geocoder's own deadline, short enough to retry


def _geocode_then_price(db: Session, contact_id: uuid.UUID, quote_id: uuid.UUID) -> Result:
    """No pin: look the typed address up, off every lock and outside any open transaction, then price the quote.

    Only a confident result inside the booking's own city and state is used (and the booking is not held);
    anything else, including a timeout or an error, is the placeholder and the hold. After the lookup the
    conversation and then the quote are locked (the usual order) and re-checked, because the person may have
    cancelled, been handed to a person or raised an emergency while it ran; the quote and the conversation
    then change in one transaction. A crash here gets a neutral message, never the payment-link error."""
    try:
        return _geocode_then_price_inner(db, contact_id, quote_id)
    except Exception:
        db.rollback()
        logger.exception("whatsapp geocode step failed for quote %s", quote_id)
        try:
            SupportService(db).open(contact_id=contact_id, reason="booking_placeholder", priority="normal")
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("whatsapp could not open a follow-up ticket for quote %s", quote_id)
        lang = db.get(WhatsAppContact, contact_id).language
        return Result([copy.text(lang, "quote_pending_body")])


def _geocode_then_price_inner(db: Session, contact_id: uuid.UUID, quote_id: uuid.UUID) -> Result:
    now = datetime.utcnow()
    quote = (db.query(Quote).filter(Quote.id == quote_id, Quote.contact_id == contact_id)
             .populate_existing().with_for_update().first())
    if quote is None or quote.status != "submitted":
        db.rollback()
        return Result()
    payload = dict(quote.payload or {})
    started = payload.get("geocode_in_flight")
    try:
        in_flight = started is not None and now - datetime.fromisoformat(started) < GEOCODE_IN_FLIGHT_TTL
    except (TypeError, ValueError):
        in_flight = False
    if in_flight:  # another message already has a lookup running for this quote: it will answer
        db.rollback()
        return Result()
    address, city = payload.get("address"), payload.get("city")
    state = payload.get("state") or rules.CITY_STATE.get(city or "", city or "")
    payload["geocode_in_flight"] = now.isoformat()
    quote.payload = payload
    db.commit()  # releases the lock and ends the transaction: nothing is held across the HTTP call

    found = None
    try:
        found = build_geocoder().geocode(str(address), city=str(city), state=str(state))
    except Exception:  # the geocoder promises not to raise; a bug in it must not lose the booking
        logger.exception("whatsapp geocoder raised for quote %s; treated as no result", quote_id)
    if found is not None and not (rules.in_city(city, found.lat, found.lng)
                                  and rules.state_matches(city, found.admin_area)):
        logger.info("whatsapp quote %s: geocoded address is not in %s; held for an address check", quote_id, city)
        found = None

    conversation = (db.query(WhatsAppConversation).filter_by(contact_id=contact_id).populate_existing()
                    .with_for_update().one())
    quote = (db.query(Quote).filter(Quote.id == quote_id, Quote.contact_id == contact_id)
             .populate_existing().with_for_update().first())
    still_waiting = (State(conversation.state) == State.AWAITING_LOCATION
                     and (conversation.context or {}).get("quote_id") == str(quote_id))
    if quote is None or quote.status != "submitted" or not still_waiting:
        if quote is not None and quote.status == "submitted":  # the lookup is over; nobody else is waiting on it
            quote.payload = {k: v for k, v in (quote.payload or {}).items() if k != "geocode_in_flight"}
            db.commit()
        else:
            db.rollback()
        return Result()
    payload = {k: v for k, v in (quote.payload or {}).items() if k != "geocode_in_flight"}
    if found is not None:
        payload.update(lat=found.lat, lng=found.lng, needs_address_check=False, location_source="geocoded")
    else:
        _use_placeholder(payload)
    quote.payload = payload
    logger.info("whatsapp quote %s location source=%s precision=%s", quote_id, payload["location_source"],
                found.precision if found else "none")
    contact = db.get(WhatsAppContact, contact_id)
    result = _price(Ctx(db, contact, conversation, now), quote)
    if result.state is not None:  # the state change rides in the same commit as the quote's
        merged = {**(conversation.context or {}), **result.context}
        conversation.context = {k: v for k, v in merged.items() if v is not None}
        conversation.state = result.state.value
        conversation.automation_paused = result.state in PAUSED_STATES
        conversation.updated_at = datetime.utcnow()
    db.commit()
    return Result(result.replies)  # state and context are already stored; nothing left for apply_after_commit


def _recipient(ctx: Ctx, quote: Quote) -> Optional[CareRecipient]:
    """The recipient of this quote, only if the contact is really related to them (never trust a stored id)."""
    recipient_id = _uuid((quote.payload or {}).get("recipient_id"))
    if recipient_id is None or ctx.contact.user_id is None:
        return None
    return (ctx.db.query(CareRecipient).join(CareRelationship, CareRelationship.recipient_id == CareRecipient.id)
            .filter(CareRecipient.id == recipient_id, CareRelationship.contact_user_id == ctx.contact.user_id)
            .first())


def _breakdown(price: PriceBreakdown) -> Dict[str, int]:
    return {"base_calculation_kobo": price.base_calculation_kobo, "surge_adjustment_kobo": price.surge_adjustment_kobo,
            "discount_savings_kobo": price.discount_savings_kobo, "service_fee_kobo": price.service_fee_kobo,
            "platform_fee_kobo": price.platform_fee_kobo, "total_kobo": price.total_kobo}


def _price(ctx: Ctx, quote: Quote) -> Result:
    payload = dict(quote.payload or {})
    row = ctx.db.query(BookingSettings).order_by(BookingSettings.created_at.desc()).first()
    try:
        if row is None:
            raise ValueError("booking settings not configured")
        price = price_service(row, payload["service"], int(payload["duration_hours"]))  # the same compute_price
    except (KeyError, ValueError):
        logger.error("whatsapp quote %s could not be priced (service %s)", quote.id, payload.get("service"))
        close_quote(quote, "cancelled")
        SupportService(ctx.db).open(contact_id=ctx.contact.id, reason="booking_placeholder", priority="normal")
        return Result([_text(ctx, "quote_no_price_body")], State.HUMAN, {"quote_id": None})
    payload["breakdown"] = _breakdown(price)
    quote.payload = payload
    quote.settings_id = row.id
    quote.total_kobo = price.total_kobo
    quote.expires_at = ctx.now + timedelta(minutes=settings.WHATSAPP_QUOTE_TTL_MINUTES)
    quote.status = "quoted"
    return Result([quote_message(ctx, quote)], State.QUOTED, {"quote_id": str(quote.id)})


def quote_message(ctx: Ctx, quote: Quote) -> ButtonsMessage:
    """The itemised quote (WA-FR-19): every figure from the stored breakdown, nothing recomputed, nothing the
    person typed in the Flow other than the first name of the person being visited (PRD 9.2)."""
    payload = quote.payload or {}
    b = payload["breakdown"]
    start = rules.lagos_to_utc(date.fromisoformat(payload["date"]), payload["time"])
    recipient = _recipient(ctx, quote)
    is_self = payload.get("who") == "self" or recipient is None
    if is_self:
        intro = copy.t(ctx.lang, "quote_intro_self")
    else:
        intro = copy.t(ctx.lang, "quote_intro_other", name=recipient.full_name.split()[0][:30])
    lines = [intro, copy.t(ctx.lang, "quote_service_line", service=rules.SERVICE_LABELS[payload["service"]],
                           when=rules.format_when(start, int(payload["duration_hours"]))), ""]
    width = max(len(copy.t(ctx.lang, k)) for k in ("quote_service_fee", "quote_platform_fee", "quote_total"))

    def row(label: str, kobo: int, sign: str = "") -> str:
        return f"{label.ljust(width)}  {sign}{rules.naira(abs(kobo))}"

    if b["surge_adjustment_kobo"] or b["discount_savings_kobo"]:
        lines.append(row(copy.t(ctx.lang, "quote_base", hours=payload["duration_hours"]), b["base_calculation_kobo"]))
        if b["surge_adjustment_kobo"]:
            lines.append(row(copy.t(ctx.lang, "quote_surge"), b["surge_adjustment_kobo"], "+"))
        if b["discount_savings_kobo"]:
            lines.append(row(copy.t(ctx.lang, "quote_discount"), b["discount_savings_kobo"], "-"))
    lines += [row(copy.t(ctx.lang, "quote_service_fee"), b["service_fee_kobo"]),
              row(copy.t(ctx.lang, "quote_platform_fee"), b["platform_fee_kobo"]),
              row(copy.t(ctx.lang, "quote_total"), b["total_kobo"]), "",
              copy.t(ctx.lang, "quote_cancellation", hours=settings.WHATSAPP_CANCEL_FREE_HOURS)]
    return ButtonsMessage("\n".join(lines), (
        ("quote:confirm", copy.t(ctx.lang, "quote_confirm")), ("quote:change", copy.t(ctx.lang, "quote_change")),
        ("quote:cancel", copy.t(ctx.lang, "quote_cancel"))))


def show_quote(ctx: Ctx) -> Result:
    quote = _lock_quote(ctx, ctx.quote_id)
    if quote is None or quote.status != "quoted":
        return _stale(ctx)
    if quote.expires_at is None or quote.expires_at <= ctx.now:
        close_quote(quote, "expired")
        return _to_idle(_menu(ctx, "quote_expired_body"))
    return Result([quote_message(ctx, quote)], State.QUOTED)


def change_quote(ctx: Ctx) -> Result:
    # Nothing is created and nothing charged: start a new draft (this closes the old quote).
    return start_booking(ctx)


# --- confirming: the booking ------------------------------------------------------------------
def confirm_quote(ctx: Ctx) -> Result:
    quote = _lock_quote(ctx, ctx.quote_id)
    if quote is None:
        return _stale(ctx)
    if quote.booking_id is not None:  # a second tap, or a retry: the booking exists, never make another
        return _existing_booking(ctx, quote)
    if quote.status != "quoted" or quote.expires_at is None or quote.expires_at <= ctx.now:
        if quote.status in OPEN_QUOTE_STATUSES:
            close_quote(quote, "expired")
        return _to_idle(_menu(ctx, "quote_expired_body"))

    payload = quote.payload or {}
    start = rules.lagos_to_utc(date.fromisoformat(payload["date"]), payload["time"])
    recipient = _recipient(ctx, quote)
    patient = ctx.db.query(Patient).filter(Patient.user_id == ctx.contact.user_id).first()
    if recipient is None or patient is None or start <= ctx.now or not quote.sensitive_encrypted:
        close_quote(quote, "expired")
        return _to_idle(_menu(ctx, "quote_expired_body"))

    secrets = json.loads(decrypt(quote.sensitive_encrypted))
    b = payload["breakdown"]
    locked = PriceBreakdown(b["base_calculation_kobo"], b["surge_adjustment_kobo"], b["discount_savings_kobo"],
                            b["service_fee_kobo"], b["platform_fee_kobo"], b["total_kobo"])
    data = BookingCreate(
        service_type=payload["service"], assistance_type=rules.SERVICE_LABELS[payload["service"]],
        condition=None, duration_hours=int(payload["duration_hours"]), scheduled_date=start,
        patient_address=payload["address"], patient_latitude=payload["lat"], patient_longitude=payload["lng"],
        patient_city=payload["city"], patient_state=payload["state"],
        special_instructions=secrets.get("instructions"), booking_for=_booking_for(payload, recipient),
    )
    booking = BookingService(ctx.db).insert_booking(
        patient.id, data, commit=False, locked_price=locked, channel="whatsapp",
        booked_by_user_id=ctx.contact.user_id, care_recipient_id=recipient.id,
        condition_encrypted=encrypt(secrets["condition"]), needs_address_check=bool(payload.get("needs_address_check")),
    )
    quote.booking_id = booking.id
    quote.status = "confirmed"
    # The encrypted answers stay on the quote until the payment link exists, so a failed link can go back to the
    # quote without asking again; ``_open_payment`` wipes them.
    context = {"quote_id": str(quote.id), "booking_id": str(booking.id)}
    booking_id, contact_id, quote_id = booking.id, ctx.contact.id, quote.id  # plain values: the objects expire on commit
    return Result(state=State.AWAITING_PAYMENT, context=context,
                  after_commit=lambda db: _open_payment(db, contact_id, booking_id, quote_id))


def _booking_for(payload: Dict[str, Any], recipient: CareRecipient) -> str:
    if payload.get("who") == "self":
        return "myself"
    try:
        return str(json.loads(decrypt(recipient.notes_encrypted)).get("relationship") or "family")
    except Exception:  # an older or empty note is not a reason to fail a booking
        return "family"


def _existing_booking(ctx: Ctx, quote: Quote) -> Result:
    booking = ctx.db.get(Booking, quote.booking_id)
    if booking is None:
        return _stale(ctx)
    contact_id, booking_id = ctx.contact.id, booking.id
    return Result(state=State.AWAITING_PAYMENT, context={"quote_id": str(quote.id), "booking_id": str(booking_id)},
                  after_commit=lambda db: check_payment_after_commit(db, contact_id, booking_id))


def _set_conversation(db: Session, contact_id: uuid.UUID, state: State, context: Dict[str, Any]) -> None:
    conversation = (db.query(WhatsAppConversation).filter_by(contact_id=contact_id).populate_existing()
                    .with_for_update().one())
    merged = {**(conversation.context or {}), **context}
    conversation.context = {k: v for k, v in merged.items() if v is not None}
    conversation.state = state.value
    conversation.updated_at = datetime.utcnow()


def _open_payment(db: Session, contact_id: uuid.UUID, booking_id: uuid.UUID, quote_id: uuid.UUID) -> Result:
    """After the booking is committed: ask Paystack for the checkout link and send "Pay now" (WA-FR-22)."""
    contact = db.get(WhatsAppContact, contact_id)
    lang = contact.language
    booking = db.query(Booking).filter(Booking.id == booking_id).populate_existing().first()
    try:
        reply = BookingService(db).start_payment(booking, callback_url=_return_url(booking))
    except Exception:
        logger.exception("whatsapp payment init crashed for booking %s", booking_id)
        reply = {"payment_url": None}
    if not reply.get("payment_url"):
        return _payment_failed(db, contact, booking, quote_id)
    # Lock order everywhere: conversation, then quote, then booking. This path holds no conversation lock, so it
    # only ever takes quote then booking, which cannot form a cycle with the others.
    quote = db.query(Quote).filter(Quote.id == quote_id).populate_existing().with_for_update().first()
    payload = dict(quote.payload or {})
    payload.update(payment_url=reply["payment_url"], access_code=reply.get("access_code"))
    quote.payload = payload
    quote.sensitive_encrypted = None
    booking.payment_expires_at = datetime.utcnow() + timedelta(minutes=settings.PAYMENT_LINK_TTL_MINUTES)
    db.commit()
    return Result([pay_message(lang, reply["payment_url"])], State.AWAITING_PAYMENT)


def _return_url(booking: Booking) -> str:
    return f"{settings.WHATSAPP_PAYMENT_RETURN_URL}?ref={booking.booking_code}"


def pay_message(lang: str, url: str) -> CtaUrlMessage:
    return CtaUrlMessage(copy.t(lang, "pay_body", minutes=str(settings.PAYMENT_LINK_TTL_MINUTES)),
                         copy.t(lang, "pay_cta"), url)


def _payment_failed(db: Session, contact: WhatsAppContact, booking: Booking, quote_id: uuid.UUID) -> Result:
    """No checkout link: the booking can never be paid, so it is cancelled and the person goes back to the same
    locked quote to try again. ``start_payment`` has already marked the booking cancelled and failed."""
    _set_conversation(db, contact.id, State.QUOTED, {"booking_id": None})  # conversation lock first
    quote = db.query(Quote).filter(Quote.id == quote_id).populate_existing().with_for_update().first()
    booking = db.query(Booking).filter(Booking.id == booking.id).populate_existing().with_for_update().first()
    if booking.payment_status == PaymentStatus.PENDING:
        booking.payment_status = PaymentStatus.FAILED
        booking.status = BookingStatus.CANCELLED
    if booking.cancel_reason is None:
        booking.cancel_reason = "payment_init_failed"
    quote.booking_id = None
    quote.status = "quoted"
    db.commit()
    replies: List[OutboundMessage] = [copy.text(contact.language, "pay_failed_body")]
    if quote.expires_at is not None and quote.expires_at > datetime.utcnow() and quote.sensitive_encrypted:
        ctx = Ctx(db, contact, db.query(WhatsAppConversation).filter_by(contact_id=contact.id).one(),
                  datetime.utcnow())
        replies.append(quote_message(ctx, quote))
    return Result(replies, State.QUOTED)


# --- after confirming: paying, checking, cancelling -------------------------------------------
def check_payment(ctx: Ctx) -> Result:
    booking_id = _uuid((ctx.conversation.context or {}).get("booking_id"))
    booking = ctx.db.get(Booking, booking_id) if booking_id else None
    if booking is None:
        return _to_idle(_menu(ctx, "flow_stale_body"))
    contact_id = ctx.contact.id
    if booking.payment_status == PaymentStatus.PAID:
        return Result(after_commit=lambda db: check_payment_after_commit(db, contact_id, booking_id))
    if booking.status == BookingStatus.CANCELLED:
        if booking.cancel_reason == "unpaid_timeout":
            # Expired, but they may have paid on the link and the webhook may have been lost: ask Paystack
            # before telling them it is cancelled.
            return Result(after_commit=lambda db: check_payment_after_commit(db, contact_id, booking_id))
        # Failed or cancelled by them: the cancellation message comes from the booking event; the state just goes back.
        return _to_idle(_menu(ctx, "cancelled_body"))
    return Result(after_commit=lambda db: check_payment_after_commit(db, contact_id, booking_id))


def check_payment_after_commit(db: Session, contact_id: uuid.UUID, booking_id: uuid.UUID) -> Result:
    """The person came back to the chat. If the webhook has not arrived yet, ask Paystack (WA-FR-27) before
    answering; settlement is the same function the webhook uses, so the two cannot disagree."""
    from app.channels.whatsapp import notifications  # imported here: notifications imports this module's friends
    from app.services.booking_payment import SettleOutcome, verify_and_settle

    contact = db.get(WhatsAppContact, contact_id)
    lang = contact.language
    booking = db.query(Booking).filter(Booking.id == booking_id).populate_existing().first()
    quote = db.query(Quote).filter(Quote.booking_id == booking_id).first()
    url = (quote.payload or {}).get("payment_url") if quote else None

    expired_unpaid = booking.status == BookingStatus.CANCELLED and booking.cancel_reason == "unpaid_timeout"
    if booking.payment_status != PaymentStatus.PAID and (booking.status == BookingStatus.PENDING or expired_unpaid):
        if booking.status == BookingStatus.PENDING and not url:
            age = datetime.utcnow() - booking.created_at
            if age < PAYMENT_INIT_GRACE:
                return Result([copy.text(lang, "pay_preparing_body")], State.AWAITING_PAYMENT)
            return _payment_failed(db, contact, booking, quote.id) if quote else Result()
        outcome = verify_and_settle(db, booking_id)
        if outcome is not None:
            logger.info("whatsapp booking %s verified on return: %s", booking_id, outcome.value)
            if outcome == SettleOutcome.UNREACHABLE and expired_unpaid:
                return Result([copy.text(lang, "pay_check_failed_body")], State.AWAITING_PAYMENT)
            if outcome == SettleOutcome.AMOUNT_MISMATCH:
                return Result([copy.text(lang, "pay_waiting_body", minutes=str(settings.PAYMENT_LINK_TTL_MINUTES))],
                              State.AWAITING_PAYMENT)
        db.expire_all()
        booking = db.query(Booking).filter(Booking.id == booking_id).populate_existing().first()

    if booking.payment_status == PaymentStatus.PAID:
        notifications.deliver_pending(db, booking_id=booking_id)  # payment_received goes out now, once
        return Result(state=State.IDLE, context={"quote_id": None, "booking_id": None})
    if booking.status == BookingStatus.CANCELLED:
        return Result([copy.menu(lang, "cancelled_body")], State.IDLE, {"quote_id": None, "booking_id": None})
    if url:
        return Result([_waiting_message(lang, url)], State.AWAITING_PAYMENT)
    return Result([copy.text(lang, "pay_preparing_body")], State.AWAITING_PAYMENT)


def _waiting_message(lang: str, url: str) -> CtaUrlMessage:
    return CtaUrlMessage(copy.t(lang, "pay_waiting_body", minutes=str(settings.PAYMENT_LINK_TTL_MINUTES)),
                         copy.t(lang, "pay_cta"), url)


def cancel_unpaid(ctx: Ctx) -> Result:
    """Cancel before paying. The person may have paid on the link a moment ago with the webhook still on its way,
    so Cancel is only honoured after asking Paystack (``_cancel_after_check``, off the conversation lock)."""
    context = {k: v for k, v in (ctx.conversation.context or {}).items() if k in ("quote_id", "booking_id")}
    booking_id = _uuid(context.get("booking_id"))
    booking = ctx.db.get(Booking, booking_id) if booking_id else None
    if booking is None:
        return _to_idle(_menu(ctx, "flow_stale_body"))
    if booking.payment_status == PaymentStatus.PAID:
        return Result([_text(ctx, "pay_cancel_paid_body")], State.AWAITING_PAYMENT, context)
    contact_id = ctx.contact.id
    return Result(state=State.AWAITING_PAYMENT, context=context,
                  after_commit=lambda db: _cancel_after_check(db, contact_id, booking_id))


def _cancel_after_check(db: Session, contact_id: uuid.UUID, booking_id: uuid.UUID) -> Result:
    from app.channels.whatsapp import notifications  # imported here: notifications imports this module
    from app.services.booking_payment import SettleOutcome, verify_and_settle

    lang = db.get(WhatsAppContact, contact_id).language
    outcome = verify_and_settle(db, booking_id)
    if outcome == SettleOutcome.UNREACHABLE:
        # Not knowing is not the same as "not paid": cancelling now could cancel a paid booking.
        return Result([copy.text(lang, "pay_check_failed_body")], State.AWAITING_PAYMENT)
    if outcome == SettleOutcome.AMOUNT_MISMATCH:
        # Money was taken but not the right amount: do not cancel it away, a person has to look.
        return Result([copy.text(lang, "pay_cancel_paid_body")], State.AWAITING_PAYMENT)
    # Only the booking row is locked here (the conversation lock was released at the commit), so the lock order
    # conversation -> quote -> booking holds. The state is re-read under the lock: a webhook may have won.
    booking = db.query(Booking).filter(Booking.id == booking_id).populate_existing().with_for_update().first()
    if booking.payment_status == PaymentStatus.PAID:
        db.rollback()
        notifications.deliver_pending(db, booking_id=booking_id)  # "payment received" (or the refund notice) now
        return Result(state=State.IDLE, context={"quote_id": None, "booking_id": None})
    if booking.status == BookingStatus.PENDING:
        booking.status = BookingStatus.CANCELLED
        booking.cancel_reason = "user_cancelled"
    db.commit()
    return Result([copy.menu(lang, "pay_cancelled_body")], State.IDLE,
                  {"quote_id": None, "booking_id": None, "waitlist_quote_id": None})


cancel_unpaid_after_check = _cancel_after_check  # the same step, for "My bookings" (W3)


# --- dispatcher and housekeeping --------------------------------------------------------------
def apply(ctx: Ctx, effect: Any) -> Result:
    if isinstance(effect, StartBooking):
        return start_booking(ctx)
    if isinstance(effect, SubmitFlow):
        return submit_flow(ctx, effect)
    if isinstance(effect, SharedLocation):
        return shared_location(ctx, effect)
    if isinstance(effect, ResumeDraft):
        return resume_draft(ctx)
    if isinstance(effect, ShowQuote):
        return show_quote(ctx)
    if isinstance(effect, ConfirmQuote):
        return confirm_quote(ctx)
    if isinstance(effect, ChangeQuote):
        return change_quote(ctx)
    if isinstance(effect, CancelDraft):
        return cancel_draft(ctx)
    if isinstance(effect, CheckPayment):
        return check_payment(ctx)
    if isinstance(effect, CancelUnpaid):
        return cancel_unpaid(ctx)
    if isinstance(effect, JoinWaitlist):
        return join_waitlist(ctx, effect)
    raise TypeError(f"not a booking effect: {type(effect).__name__}")


EXPIRABLE_STATES = (State.BOOKING_FLOW, State.AWAITING_LOCATION, State.QUOTED)


def idle_expired(conversation: WhatsAppConversation, now: datetime) -> bool:
    """An unfinished booking conversation (not one with a created booking: that has its own 30 minute payment
    expiry) that has been silent past the limit (WA-FR-12)."""
    if State(conversation.state) not in EXPIRABLE_STATES or conversation.updated_at is None:
        return False
    return now - conversation.updated_at > timedelta(minutes=settings.WHATSAPP_IDLE_EXPIRY_MINUTES)


def expire_idle(db: Session, conversation: WhatsAppConversation) -> None:
    close_open_quotes(db, conversation.contact_id, "expired")


def apply_after_commit(db: Session, contact_id: uuid.UUID, result: Result) -> Result:
    """Persist the state a follow-up asks for (its own transaction, conversation row locked)."""
    if result.state is not None or result.context:
        _set_conversation(db, contact_id, result.state or State(
            db.query(WhatsAppConversation).filter_by(contact_id=contact_id).one().state), result.context)
        db.commit()
    return result
