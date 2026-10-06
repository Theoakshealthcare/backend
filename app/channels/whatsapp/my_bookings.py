"""My bookings in chat (W3, WA-FR-38 and WA-FR-39): list, details, and cancelling.

The effects are stateless: every button carries the booking it is about (``bk:view:<id>``), and every one is
re-checked here against the person's own bookings, so a forged or stale id can never reveal or cancel somebody
else's booking. A booking is *visible* to a contact when they booked it, when it is for a person they care for
(a ``care_relationships`` row), or when it is their own app booking. Only the booker (or the patient who made the
app booking) may *cancel*: someone merely related to the recipient is pointed at the team.

What a detail shows: code, service, date and time, who it is for (a first name), status, total, the caregiver's
first name, and the visit code to the booker while a caregiver is assigned. Never the condition, the special
instructions or the address (WA-NFR-6).

Cancelling follows ``app.services.booking_cancellation``; the policy is shown before the person confirms. An unpaid
booking reuses W2's verify-before-cancel step (a payment may be on its way), so a paid-but-webhook-late booking is
never cancelled away.
"""

import logging
import uuid
from typing import Any, List, Optional

from cryptography.fernet import InvalidToken
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.channels.whatsapp import booking_chat, booking_rules as rules, copy
from app.channels.whatsapp.booking_chat import Ctx, Result
from app.channels.whatsapp.engine import (
    AskCancelBooking, ConfirmCancelBooking, ListBookings, ShowBooking, State,
)
from app.channels.whatsapp.messages import (
    ButtonsMessage, ListMessage, ListRow, ListSection, OutboundMessage,
)
from app.core.config import settings
from app.core.security import decrypt
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.services import booking_cancellation as cancellation
from app.services.support import SupportService

logger = logging.getLogger(__name__)

LIST_SIZE = 5  # WA-FR-38


# --- which bookings are the person's ----------------------------------------------------------------
def _visible_filter(db: Session, user_id: uuid.UUID):
    related = select(CareRelationship.recipient_id).where(CareRelationship.contact_user_id == user_id)
    own_patient = select(Patient.id).where(Patient.user_id == user_id)
    return or_(Booking.booked_by_user_id == user_id, Booking.care_recipient_id.in_(related),
               Booking.patient_id.in_(own_patient))


def _is_booker(db: Session, booking: Booking, user_id: uuid.UUID) -> bool:
    if booking.booked_by_user_id is not None:
        return booking.booked_by_user_id == user_id
    return db.query(Patient.id).filter(Patient.id == booking.patient_id, Patient.user_id == user_id).first() is not None


def recent_bookings(db: Session, user_id: uuid.UUID, limit: int = LIST_SIZE) -> List[Booking]:
    return (db.query(Booking).filter(_visible_filter(db, user_id))
            .order_by(Booking.created_at.desc(), Booking.id).limit(limit).all())


def _find(ctx: Ctx, raw_id: str) -> Optional[Booking]:
    try:
        booking_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    return (ctx.db.query(Booking).filter(Booking.id == booking_id, _visible_filter(ctx.db, ctx.contact.user_id))
            .populate_existing().first())


# --- display --------------------------------------------------------------------------------------------
def status_label(lang: str, booking: Booking) -> str:
    status, paid = booking.status, booking.payment_status == PaymentStatus.PAID
    if status == BookingStatus.PENDING:
        return copy.t(lang, "status_finding" if paid else "status_awaiting_payment")
    key = {BookingStatus.ACCEPTED: "status_assigned", BookingStatus.EN_ROUTE: "status_en_route",
           BookingStatus.IN_PROGRESS: "status_in_progress", BookingStatus.COMPLETED: "status_completed",
           BookingStatus.CANCELLED: "status_cancelled", BookingStatus.DECLINED: "status_declined"}[status]
    return copy.t(lang, key)


def _service(booking: Booking) -> str:
    return rules.SERVICE_LABELS.get(booking.service_type, booking.service_type.capitalize())


def _when(booking: Booking) -> str:
    start = rules.to_lagos(booking.scheduled_date)
    return f"{start:%a} {start.day} {start:%b}, {start:%H:%M}"


def _recipient_line(db: Session, lang: str, booking: Booking) -> Optional[str]:
    if booking.care_recipient_id is None:
        return None
    recipient = db.get(CareRecipient, booking.care_recipient_id)
    parts = (recipient.full_name if recipient else "").split()
    if not parts:
        return None
    return copy.t(lang, "booking_detail_for", name=parts[0])


def list_message(ctx: Ctx) -> OutboundMessage:
    bookings = recent_bookings(ctx.db, ctx.contact.user_id)
    if not bookings:
        return copy.menu(ctx.lang, "bookings_none_body")
    rows = tuple(ListRow(
        f"bk:view:{b.id}", b.booking_code,
        copy.t(ctx.lang, "bookings_row_description", service=_service(b), when=_when(b),
               status=status_label(ctx.lang, b))[:72])
        for b in bookings)
    return ListMessage(copy.t(ctx.lang, "bookings_list_body"), copy.t(ctx.lang, "bookings_list_button"),
                       (ListSection(copy.t(ctx.lang, "bookings_section"), rows),))


def _visit_code(booking: Booking) -> Optional[str]:
    if booking.status not in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE) or not booking.visit_code_enc:
        return None
    try:
        return decrypt(booking.visit_code_enc)
    except InvalidToken:
        logger.error("booking %s: visit code cannot be decrypted; not shown", booking.booking_code)
        return None


def detail_message(ctx: Ctx, booking: Booking) -> OutboundMessage:
    lang, db = ctx.lang, ctx.db
    booker = _is_booker(db, booking, ctx.contact.user_id)
    lines = [copy.t(lang, "booking_detail_head", code=booking.booking_code),
             copy.t(lang, "booking_detail_when", service=_service(booking), when=_when(booking)),
             _recipient_line(db, lang, booking) or copy.t(lang, "booking_detail_for_self"),
             copy.t(lang, "booking_detail_status", status=status_label(lang, booking)),
             copy.t(lang, "booking_detail_total", total=rules.naira(booking.total_amount_kobo))]
    if booking.caregiver_id and booking.status in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE,
                                                   BookingStatus.IN_PROGRESS, BookingStatus.COMPLETED):
        name = db.query(Caregiver.full_name).filter(Caregiver.id == booking.caregiver_id).scalar() or ""
        if name.split():
            lines.append(copy.t(lang, "booking_detail_caregiver", name=name.split()[0]))
    code = _visit_code(booking) if booker else None
    if code:
        lines.append(copy.t(lang, "booking_detail_code", code=code))
    if booking.status == BookingStatus.CANCELLED and booking.refund_required:
        amount = booking.refund_amount_kobo if booking.refund_amount_kobo is not None else booking.total_amount_kobo
        lines.append(copy.t(lang, "booking_detail_refund", amount=rules.naira(amount)))
    back = ("bk:list", copy.t(lang, "bookings_back"))
    status = booking.status
    if booker and status in (BookingStatus.PENDING, BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE):
        buttons = ((f"bk:cancel:{booking.id}", copy.t(lang, "bookings_cancel_button")), back)
    elif status == BookingStatus.COMPLETED:
        buttons = (("menu:book", copy.t(lang, "bookings_book_again")), back)
    elif status == BookingStatus.IN_PROGRESS:
        buttons = (("menu:human", copy.t(lang, "bookings_talk")), back)
    else:
        buttons = (back,)
    return ButtonsMessage("\n".join(lines), buttons)


# --- effects -------------------------------------------------------------------------------------------
def apply(ctx: Ctx, effect: Any) -> Result:
    if isinstance(effect, ListBookings):
        return Result([list_message(ctx)], State.IDLE)
    booking = _find(ctx, effect.booking_id)
    if booking is None:
        return Result([copy.text(ctx.lang, "bookings_not_found_body"), list_message(ctx)], State.IDLE)
    if isinstance(effect, ShowBooking):
        return Result([detail_message(ctx, booking)], State.IDLE)
    if isinstance(effect, AskCancelBooking):
        return _ask_cancel(ctx, booking)
    if isinstance(effect, ConfirmCancelBooking):
        return _confirm_cancel(ctx, booking)
    raise TypeError(f"not a my-bookings effect: {type(effect).__name__}")


def _refusal(ctx: Ctx, booking: Booking, terms: cancellation.Terms) -> Result:
    lang = ctx.lang
    if terms.refusal == cancellation.REASON_IN_PROGRESS:
        SupportService(ctx.db).open(contact_id=ctx.contact.id, reason="talk_to_person", priority="normal")
        return Result([copy.text(lang, "cancel_in_progress_body")], State.HUMAN)  # WA-FR-39: handed to a human
    return Result([copy.text(lang, "cancel_closed_body", code=booking.booking_code,
                             status=status_label(lang, booking).lower())], State.IDLE)


def _ask_cancel(ctx: Ctx, booking: Booking) -> Result:
    lang = ctx.lang
    if not _is_booker(ctx.db, booking, ctx.contact.user_id):
        return Result([copy.text(lang, "cancel_not_booker_body")], State.IDLE)
    terms = cancellation.terms(booking, ctx.now)
    confirm = ((f"bk:confirm:{booking.id}", copy.t(lang, "cancel_yes")),
               (f"bk:keep:{booking.id}", copy.t(lang, "cancel_keep")))
    if terms.refusal == cancellation.REASON_UNPAID and booking.status == BookingStatus.PENDING:
        return Result([ButtonsMessage(copy.t(lang, "cancel_ask_unpaid", code=booking.booking_code), confirm)],
                      State.IDLE)
    if not terms.allowed:
        return _refusal(ctx, booking, terms)
    if terms.fee_kobo:
        text = copy.t(lang, "cancel_ask_fee", code=booking.booking_code, hours=str(settings.WHATSAPP_CANCEL_FREE_HOURS),
                      percent=str(max(0, min(100, settings.WHATSAPP_CANCEL_FEE_PERCENT))),
                      fee=rules.naira(terms.fee_kobo), refund=rules.naira(terms.refund_kobo))
    else:
        text = copy.t(lang, "cancel_ask_full", code=booking.booking_code, refund=rules.naira(terms.refund_kobo))
    return Result([ButtonsMessage(text, confirm)], State.IDLE)


def _confirm_cancel(ctx: Ctx, booking: Booking) -> Result:
    lang = ctx.lang
    if not _is_booker(ctx.db, booking, ctx.contact.user_id):
        return Result([copy.text(lang, "cancel_not_booker_body")], State.IDLE)
    code, booking_id, contact_id = booking.booking_code, booking.id, ctx.contact.id
    if booking.status == BookingStatus.PENDING and booking.payment_status == PaymentStatus.PENDING:
        return Result(state=State.IDLE, after_commit=lambda db: _cancel_unpaid(db, contact_id, booking_id))
    # Under the conversation lock the step already holds; this takes the booking lock and decides afresh.
    outcome = cancellation.cancel_by_booker(ctx.db, booking_id, ctx.now, commit=False)
    if outcome.status == "already_cancelled":
        return Result([copy.menu_with_body(lang, copy.t(lang, "cancel_already_body", code=code))], State.IDLE)
    if outcome.status == "refused":
        return _refusal(ctx, booking, outcome.terms or cancellation.Terms(False, cancellation.REASON_CLOSED))
    terms = outcome.terms
    assert terms is not None
    if terms.fee_kobo:
        text = copy.t(lang, "cancel_done_fee", code=code, refund=rules.naira(terms.refund_kobo),
                      fee=rules.naira(terms.fee_kobo))
    else:
        text = copy.t(lang, "cancel_done_refund", code=code, refund=rules.naira(terms.refund_kobo))
    return Result([copy.menu_with_body(lang, text)], State.IDLE)


def _cancel_unpaid(db: Session, contact_id: uuid.UUID, booking_id: uuid.UUID) -> Result:
    """W2's verify-before-cancel, off the conversation lock: Paystack is asked first, so a payment whose webhook is
    late is settled (and the person told) rather than cancelled away. From the chat's idle state, "still paying"
    outcomes land back in idle: there is no payment step to return to."""
    result = booking_chat.cancel_unpaid_after_check(db, contact_id, booking_id)
    if result.state == State.AWAITING_PAYMENT:
        result.state = State.IDLE
    return result
