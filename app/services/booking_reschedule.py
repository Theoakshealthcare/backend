"""Rescheduling a paid booking from the patient app (Phase 2 sign-off pack, part E). The design and the reasons are in
``docs/plans/2026-11-25-booking-reschedule.md``; this docstring is the contract.

* **Who**: the booker only (``is_booker``, the cancel rule). 403 ``not_your_booking``, 404 ``booking_not_found``.
* **When** (all re-checked under the booking row lock): paid; ``pending`` (being dispatched, unmatched) or ``accepted``;
  the visit is MORE than ``reschedule_free_hours`` away (exactly that many hours is refused); fewer than
  ``reschedule_max_free`` reschedules used. The new time is different, MORE than ``reschedule_free_hours`` from now and at
  most ``reschedule_max_days_ahead`` days from now (exactly that many days is allowed). All three are admin settings.
* **What it does**: moves the visit, releases the assigned caregiver (visit code, on-my-way, arrived and no-show marks
  cleared; a notice row and a push), closes every stale offer, and runs the normal wave dispatch again. Nobody accepting
  ends in the admin unmatched queue by the existing escalation. Not a drop-out: no ``caregiver_dropouts`` row.
* **Price**: recomputed with the pricing engine at the live settings. The booking KEEPS the price it was paid at. A lower
  price is recorded as ``credit_due`` on the reschedule row (Finance refunds it once the visit is completed or cancelled);
  a higher one is refused (409 ``price_increase_needs_support``). Nothing here moves money.
* **Idempotent**: the same ``request_id``, or a request whose target equals the time the last reschedule already set,
  replays the stored result (``already_rescheduled`` true) with no second count and no second wave.
* **Locks**: the booking row first, nothing else (no caregiver lock: accept takes booking then caregiver, so no cycle).
  Pushes go out after the commit.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.money import kobo_to_naira
from app.db.models.booking import (
    Booking, BookingNotification, BookingReschedule, BookingDispatchWave, BookingStatus, PaymentStatus,
)
from app.db.models.caregiver import Caregiver
from app.db.models.user import User
from app.services import booking_policy, dispatch, dropout, service_catalog
from app.services import push as push_service
from app.services.booking import BookingSettingsNotConfigured, _no_settings
from app.services.patient_booking_cancel import PatientCancelError, is_booker
from app.services.pricing import compute_price, service_multiplier

logger = logging.getLogger(__name__)

HANDLING_NONE, HANDLING_CREDIT_DUE = "none", "credit_due"
RESCHEDULED = "rescheduled"  # the response a stale offer is closed with; the accept path refuses it
KEPT_OFFER_RESPONSES = ("declined", RESCHEDULED)  # a decline stays final; everything else was an offer for the OLD time
REQUEST_ID_MAX = 64

_NOT_RESCHEDULABLE = {
    BookingStatus.EN_ROUTE: "The caregiver is already on the way, so the visit cannot be rescheduled in the app. Please contact support.",
    BookingStatus.IN_PROGRESS: "The visit has already started, so it cannot be rescheduled.",
    BookingStatus.COMPLETED: "This visit is already completed, so it cannot be rescheduled.",
    BookingStatus.DECLINED: "This booking can no longer be rescheduled.",
}


def _error(status_code: int, code: str, message: str, **extra: Any) -> PatientCancelError:
    return PatientCancelError(status_code, code, message, **extra)


def naive_utc(value: datetime) -> datetime:
    """The schema stores naive UTC; a client may send an offset (``...Z``): convert, then drop the zone."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# --- the price at the new time ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Quote:
    paid_kobo: int
    new_kobo: int

    @property
    def difference_kobo(self) -> int:
        return self.new_kobo - self.paid_kobo   # negative: the family paid more than the price now

    @property
    def handling(self) -> str:
        return HANDLING_CREDIT_DUE if self.difference_kobo < 0 else HANDLING_NONE

    def as_dict(self) -> Dict[str, Any]:
        credit = max(0, -self.difference_kobo)
        return {
            "paid_amount": kobo_to_naira(self.paid_kobo), "paid_amount_kobo": self.paid_kobo,
            "new_amount": kobo_to_naira(self.new_kobo), "new_amount_kobo": self.new_kobo,
            "difference_amount": kobo_to_naira(self.difference_kobo), "difference_kobo": self.difference_kobo,
            "handling": self.handling,
            "credit_amount": kobo_to_naira(credit), "credit_kobo": credit,
        }


def quote_for(db: Session, booking: Booking) -> Quote:
    """The booking's price recomputed with the live settings (the same call as creating a booking). 503 when no pricing
    exists, 409 ``service_unavailable`` when the service is no longer on offer or the length is no longer allowed."""
    settings = service_catalog.latest_settings(db)
    if settings is None:
        _no_settings()  # logs the loud explanation
        raise _error(503, "booking_settings_not_configured", "Pricing is not available right now. Please try again later.")
    try:
        entry = service_catalog.get_bookable(db, booking.service_type, settings)
        service_catalog.check_duration(entry, int(booking.duration_minutes))
    except (service_catalog.ServiceUnavailable, service_catalog.InvalidDuration):
        raise _error(409, "service_unavailable",
                     "This service is not available to book right now, so the visit cannot be moved in the app. "
                     "Please contact support.")
    price = compute_price(
        base_fee_kobo=settings.base_fee_kobo, minutes=int(booking.duration_minutes),
        multiplier=service_multiplier(settings, booking.service_type, channel="app"),
        surge_multiplier=settings.surge_multiplier if settings.surge_pricing else None,
        discount_percent=settings.discount, platform_fee_percent=settings.platform_fee_percent)
    return Quote(paid_kobo=int(booking.total_amount_kobo), new_kobo=price.total_kobo)


# --- what Finance owes after a lower price ---------------------------------------------------------------------------------
def latest_reschedule(db: Session, booking_id: uuid.UUID) -> Optional[BookingReschedule]:
    return (db.query(BookingReschedule).filter(BookingReschedule.booking_id == booking_id)
            .order_by(BookingReschedule.created_at.desc(), BookingReschedule.id.desc()).first())


def credit_owed_kobo(db: Session, booking_id: uuid.UUID) -> int:
    """What the family is owed back for the price difference: the **latest** reschedule's credit only. Every row is priced
    against the amount originally paid, so the rows are not cumulative (paid 10,000, quote 8,000, then quote 10,000: owed 0,
    not 2,000; 8,000 then 9,000: owed 1,000, not 3,000). The latest quote is the one the family agreed to last."""
    row = latest_reschedule(db, booking_id)
    return max(0, -int(row.price_difference_kobo)) if row is not None and row.price_handling == HANDLING_CREDIT_DUE else 0


REASON_PRICE_DIFFERENCE = "reschedule_price_difference"   # ``bookings.refund_required_reason`` (free text); the refund row's code is ``other``


def queue_credit_refund(db: Session, booking: Booking) -> Optional[Any]:
    """The visit is completed: put the promised "difference refunded after the visit" in the refund queue, so it cannot be
    forgotten. A ``pending_approval`` refund for a person to approve (nothing is paid by itself), suggested amount = the latest
    credit, never more than ``refundable_kobo``; ``booking.refund_required`` marks it. Runs in the caller's transaction under
    the booking lock. A booking cancelled instead needs nothing here: its refund is paid minus the fee, so the difference is
    already inside it. Returns the refund row, or None when nothing is owed or a refund is already open."""
    from app.services import refunds   # local: refunds imports booking_cancellation, which imports this module's neighbours

    owed = credit_owed_kobo(db, booking.id)
    if owed <= 0 or booking.payment_status not in (PaymentStatus.PAID, PaymentStatus.PARTIALLY_REFUNDED):
        return None
    refund = refunds.open_system_refund(db, booking, "other", owed)
    if refund is None:
        return None
    row = latest_reschedule(db, booking.id)
    refund.note = (f"Reschedule price difference: paid {kobo_to_naira(row.old_total_kobo)}, price at the new time "
                   f"{kobo_to_naira(row.new_total_kobo)}. The family was told this would be refunded after the visit.")
    booking.refund_required = True
    booking.refund_required_reason = REASON_PRICE_DIFFERENCE
    booking.refund_amount_kobo = refund.suggested_amount_kobo
    return refund


# --- the rules --------------------------------------------------------------------------------------------------------------
def _problem(booking: Booking, policy: booking_policy.PolicyValues, now: datetime) -> Optional[Tuple[int, str, str]]:
    """Why the BOOKING cannot be rescheduled right now, as ``(http status, code, message)``, or None."""
    if booking.status == BookingStatus.CANCELLED:
        return 409, "booking_already_cancelled", "This booking is cancelled, so it cannot be rescheduled."
    if booking.status in _NOT_RESCHEDULABLE:
        return 409, "booking_not_reschedulable", _NOT_RESCHEDULABLE[booking.status]
    if booking.payment_status == PaymentStatus.PENDING or booking.payment_status == PaymentStatus.FAILED:
        return 409, "booking_unpaid", "Pay for this booking first, or make a new booking."
    if booking.payment_status != PaymentStatus.PAID:
        return 409, "booking_not_reschedulable", "This booking can no longer be rescheduled."
    if booking.scheduled_date - now <= timedelta(hours=policy.reschedule_free_hours):
        return (409, "reschedule_window_closed",
                f"A visit can only be rescheduled more than {_hours_words(policy.reschedule_free_hours)} before it starts. "
                "You can cancel instead (the cancellation fee may apply).")
    if int(booking.reschedule_count or 0) >= policy.reschedule_max_free:
        return (409, "reschedule_limit_reached",
                f"This booking has used its {policy.reschedule_max_free} free reschedule"
                f"{'' if policy.reschedule_max_free == 1 else 's'}. Please contact support to move it again.")
    return None


def _new_time_problem(booking: Booking, new: datetime, policy: booking_policy.PolicyValues,
                      now: datetime) -> Optional[Tuple[int, str, str]]:
    if new == booking.scheduled_date:
        return 422, "same_time", "That is the time the visit already has. Please choose a different time."
    if new - now <= timedelta(hours=policy.reschedule_free_hours):
        return (422, "new_time_too_soon",
                f"The new time must be more than {_hours_words(policy.reschedule_free_hours)} from now.")
    if new - now > timedelta(days=policy.reschedule_max_days_ahead):
        return (422, "new_time_too_far",
                f"The new time can be at most {policy.reschedule_max_days_ahead} days from now. "
                "Please contact support to book further ahead.")
    return None


def _hours_words(hours: int) -> str:
    return f"{hours} hour" + ("" if hours == 1 else "s")


def _left(booking: Booking, policy: booking_policy.PolicyValues) -> int:
    return max(0, policy.reschedule_max_free - int(booking.reschedule_count or 0))


def list_info(booking: Booking, policy: booking_policy.PolicyValues, now: datetime) -> Dict[str, Any]:
    """The three fields the patient's booking list carries so the app can hide the option (no price, no queries)."""
    problem = _problem(booking, policy, now)
    return {"reschedule_count": int(booking.reschedule_count or 0), "can_reschedule": problem is None,
            "reschedules_left": _left(booking, policy)}


def attach_reschedule_info(booking_dict: Dict[str, Any], booking: Booking, policy: booking_policy.PolicyValues,
                           now: Optional[datetime] = None) -> None:
    booking_dict.update(list_info(booking, policy, now or datetime.utcnow()))


# --- the preview (GET) ------------------------------------------------------------------------------------------------------
def _load(db: Session, user: User, booking_id: uuid.UUID, *, lock: bool = False) -> Booking:
    query = db.query(Booking).filter(Booking.id == booking_id)
    if lock:
        query = query.with_for_update()
    booking = query.populate_existing().first()
    if booking is None:
        raise _error(404, "booking_not_found", "We could not find that booking.")
    if not is_booker(db, booking, user):
        raise _error(403, "not_your_booking", "Only the person who made this booking can reschedule it.")
    return booking


def terms_view(db: Session, user: User, booking_id: uuid.UUID, new_scheduled_date: Optional[datetime] = None,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """What rescheduling allows right now. Read-only and only a preview: ``reschedule`` decides again under the lock."""
    now = now or datetime.utcnow()
    booking = _load(db, user, booking_id)
    policy = booking_policy.current(db)
    problem = _problem(booking, policy, now)
    out: Dict[str, Any] = {
        "booking_id": booking.id, "booking_code": booking.booking_code,
        "can_reschedule": problem is None,
        "reason": problem[1] if problem else None, "message": problem[2] if problem else None,
        "current_scheduled_date": booking.scheduled_date,
        "free_hours": policy.reschedule_free_hours,
        "reschedules_used": int(booking.reschedule_count or 0), "reschedules_allowed": policy.reschedule_max_free,
        "reschedules_left": _left(booking, policy),
        "earliest_new_time": now + timedelta(hours=policy.reschedule_free_hours),
        "latest_new_time": now + timedelta(days=policy.reschedule_max_days_ahead),
        "caregiver_assigned": booking.caregiver_id is not None,
        "price": None, "new_time_ok": None, "new_time_reason": None, "new_time_message": None, "as_of": now,
    }
    if problem is not None:
        return out
    quote = quote_for(db, booking)
    out["price"] = quote.as_dict()
    if quote.difference_kobo > 0:
        out.update(can_reschedule=False, reason="price_increase_needs_support", message=_INCREASE_MESSAGE)
    if new_scheduled_date is not None:
        bad = _new_time_problem(booking, naive_utc(new_scheduled_date), policy, now)
        out.update(new_time_ok=bad is None, new_time_reason=bad[1] if bad else None,
                   new_time_message=bad[2] if bad else None)
    return out


_INCREASE_MESSAGE = ("The price for this visit is now higher than what you paid, so we cannot move it in the app without "
                     "charging you. Please contact support and we will arrange it with you.")


# --- the reschedule (POST) --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Result:
    booking: Booking
    row: BookingReschedule
    already: bool
    policy: booking_policy.PolicyValues

    def as_dict(self) -> Dict[str, Any]:
        b, r = self.booking, self.row
        quote = Quote(paid_kobo=int(r.old_total_kobo), new_kobo=int(r.new_total_kobo))
        return {
            "booking_id": b.id, "booking_code": b.booking_code, "status": b.status.value,
            "already_rescheduled": self.already,
            "previous_scheduled_date": r.old_scheduled_date,
            "scheduled_date": b.scheduled_date, "scheduled_end_date": b.scheduled_end_date,
            "reschedules_used": int(b.reschedule_count or 0), "reschedules_left": _left(b, self.policy),
            "caregiver_released": r.released_caregiver_id is not None,
            "dispatch_state": b.dispatch_state, "price": quote.as_dict(),
        }


def _find_replay(db: Session, booking: Booking, new: datetime, request_id: Optional[str]) -> Optional[BookingReschedule]:
    if request_id:
        row = db.query(BookingReschedule).filter(BookingReschedule.booking_id == booking.id,
                                                 BookingReschedule.request_id == request_id).first()
        if row is not None:
            if row.new_scheduled_date != new:
                raise _error(409, "request_id_reused", "That request id was already used for a different time.")
            return row
        # An unknown id is not "new" when another tap (sent without an id, or with another one) already moved the visit to
        # this very time: fall through to the same-target check below, so a double tap from two devices is a replay.
    if new != booking.scheduled_date:
        return None
    latest = (db.query(BookingReschedule).filter(BookingReschedule.booking_id == booking.id)
              .order_by(BookingReschedule.created_at.desc()).first())
    return latest if latest is not None and latest.new_scheduled_date == new else None


def reschedule(db: Session, user: User, booking_id: uuid.UUID, new_scheduled_date: datetime, *,
               request_id: Optional[str] = None, accepted_total_kobo: Optional[int] = None,
               now: Optional[datetime] = None) -> Result:
    """Move the booking for its booker. Commits (or rolls back and raises); see the module docstring."""
    now = now or datetime.utcnow()
    new = naive_utc(new_scheduled_date)
    request_id = (request_id or "").strip() or None
    if request_id is not None and len(request_id) > REQUEST_ID_MAX:
        raise _error(422, "request_id_too_long", f"The request id can be at most {REQUEST_ID_MAX} characters.")
    try:
        booking = _load(db, user, booking_id, lock=True)
        policy = booking_policy.current(db)
        replay = _find_replay(db, booking, new, request_id)
        if replay is not None:
            db.rollback()   # nothing to change: release the lock (the attributes reload on first read)
            return Result(booking, replay, True, policy)
        for problem in (_problem(booking, policy, now), _new_time_problem(booking, new, policy, now)):
            if problem is not None:
                raise _error(*problem)
        quote = quote_for(db, booking)
        if quote.difference_kobo > 0:
            raise _error(409, "price_increase_needs_support", _INCREASE_MESSAGE, price=_plain(quote.as_dict()))
        if accepted_total_kobo is not None and accepted_total_kobo != quote.new_kobo:
            raise _error(409, "price_changed", "The price changed since you last looked. Please check it again.",
                         price=_plain(quote.as_dict()))
        row, offers, push_to = _apply(db, booking, user, new, quote, request_id, now)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    db.refresh(booking)
    db.refresh(row)
    _push(db, booking, offers, push_to)
    return Result(booking, row, False, policy)


def _plain(data: Dict[str, Any]) -> Dict[str, Any]:
    """An error ``detail`` is serialised without FastAPI's encoder: Decimals must already be numbers."""
    return {k: (float(v) if hasattr(v, "quantize") else v) for k, v in data.items()}


def _apply(db: Session, booking: Booking, user: User, new: datetime, quote: Quote, request_id: Optional[str],
           now: datetime) -> Tuple[BookingReschedule, List[Dict[str, Any]], Optional[uuid.UUID]]:
    previous_caregiver = booking.caregiver_id
    old_start = booking.scheduled_date
    booking.scheduled_date = new
    booking.scheduled_end_date = new + timedelta(minutes=int(booking.duration_minutes))
    booking.reschedule_count = int(booking.reschedule_count or 0) + 1

    # The caregiver is released: nothing of that assignment may follow the booking (arrival window, no-show flag, code).
    booking.status = BookingStatus.PENDING
    booking.caregiver_id = None
    booking.visit_code_enc = None
    booking.visit_code_failed_attempts = 0
    booking.visit_code_locked_until = None
    booking.on_my_way_at = None
    booking.on_my_way_distance_m = None
    booking.arrived_at = None
    booking.no_show_flagged_at = None
    booking.no_show_flagged_by = None
    if booking.dropout_state in dropout.OPEN_STATES:
        booking.dropout_state = None   # the family chose a new time: no drop-out journey is open any more

    # Every offer for the old time is closed (the caregiver app lists unanswered offers as new requests, and the accept path
    # refuses a ``rescheduled`` one); the new wave resets the ones it re-offers.
    for offer in db.query(BookingNotification).filter(BookingNotification.booking_id == booking.id,
                                                       BookingNotification.notification_type == "new_booking"):
        if offer.response not in KEPT_OFFER_RESPONSES:   # includes auto_declined: it must not be acceptable on stale terms
            offer.response = RESCHEDULED
            offer.response_at = now

    row = BookingReschedule(
        booking_id=booking.id, requested_by_user_id=user.id, request_id=request_id, old_scheduled_date=old_start,
        new_scheduled_date=new, old_total_kobo=quote.paid_kobo, new_total_kobo=quote.new_kobo,
        price_difference_kobo=quote.difference_kobo, price_handling=quote.handling,
        released_caregiver_id=previous_caregiver, created_at=now)
    db.add(row)
    db.flush()

    offers: List[Dict[str, Any]] = []
    if booking.needs_address_check:
        booking.dispatch_state = None   # still held for an operator's address check: not dispatched, as when it was paid
    else:
        booking.dispatch_state = dispatch.DISPATCHING
        booking.dispatch_started_at = now   # restarts the escalation clock: the unmatched queue is the manual fallback
        latest = db.query(func.max(BookingDispatchWave.wave_no)).filter(
            BookingDispatchWave.booking_id == booking.id).scalar() or 0
        try:
            with db.begin_nested():
                dispatch.send_wave(db, booking, latest + 1, now, offers, renotify=True, exclude_declined=True)
        except Exception:
            offers = []
            logger.exception("booking %s rescheduled but the first wave failed; dispatch_tick will send it",
                             booking.booking_code)
    if previous_caregiver is not None:
        # After the wave: it must not mistake this notice for the caregiver's offer row (it resets the one it finds).
        db.add(BookingNotification(
            booking_id=booking.id, caregiver_id=previous_caregiver, title="Visit rescheduled",
            message="A visit you accepted was rescheduled by the patient. It is no longer assigned to you.",
            notification_type="booking_update",
            response="info"))  # informational: a non-null response keeps it out of the "new requests" list
    logger.info("booking %s rescheduled (%s -> %s), reschedule %s, caregiver released: %s, price %s",
                booking.booking_code, old_start, new, booking.reschedule_count, previous_caregiver is not None,
                quote.handling)
    return row, offers, previous_caregiver


def _push(db: Session, booking: Booking, offers: List[Dict[str, Any]], released: Optional[uuid.UUID]) -> None:
    """After the commit, best effort. Booking ids and times only: never the patient, address or condition."""
    if released is not None:
        try:
            user_id = db.query(Caregiver.user_id).filter(Caregiver.id == released).scalar()
            if user_id is not None:
                push_service.get_push_service().send_to_users(
                    db, [user_id], title="Visit rescheduled",
                    body=f"Booking {booking.booking_code} was rescheduled and is no longer assigned to you.",
                    data={"type": "booking_rescheduled", "booking_id": str(booking.id)})
        except Exception:
            db.rollback()
            logger.warning("push: reschedule notice for booking %s failed", booking.id, exc_info=True)
    if offers:
        dispatch.push_offers(db, offers)
