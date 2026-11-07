"""Cancelling a booking from the patient app (P4-BE): what it costs, the cancellation itself, and the refund summary.

The money rules live in ``app.services.booking_cancellation`` (the same ``terms`` and ``cancel_by_booker`` the WhatsApp
channel uses, so the two channels cannot disagree). This module is the app's wrapper around them:

* **Who may**: only the booker. An app booking is the patient's own (``booked_by_user_id`` is NULL); a WhatsApp booking
  belongs to whoever made it. Anyone else gets 403, an unknown id 404.
* **Unpaid booking** (payment link not used yet): Paystack is asked first, exactly like WhatsApp's verify-before-cancel,
  so a payment whose webhook is late is never cancelled away. Still unpaid: cancelled, free, nothing to refund.
* **Paid booking**: ``cancel_by_booker`` under the booking row lock. A caregiver accepting or starting at the same moment
  takes the same lock, so exactly one wins; two cancels serialise and the second one *replays* the first's result.
* **Idempotent**: a repeat of a cancel the booker already made (double tap, retry after a timeout) is a 200 with
  ``already_cancelled = true`` and the stored result. Cancelled for any other reason it is a 409.
* **Refund**: never moves money. The paid booking gets a ``pending_approval`` refund for a human to approve (RF-D8),
  amount = paid minus the tier fee, never more than was paid. The patient wallet is not offered: the destination the
  app is told about is always the original payment method.

``terms_changed``: the app sends the fee the person saw (``accepted_fee_kobo``). If the fee decided under the lock is
higher (a caregiver accepted, or the 12 h line passed, since they looked) nothing is cancelled and the 409 carries the new
terms.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.core.money import kobo_to_naira
from app.db.models.booking import PAID_STATES, Booking, BookingStatus, PaymentStatus
from app.db.models.patient import Patient
from app.db.models.refund import Refund
from app.db.models.user import User
from app.services import booking_cancellation as cancellation
from app.services.notification_service import NotificationService
from app.services.refunds import refundable_kobo

logger = logging.getLogger(__name__)

USER_CANCELLED = "user_cancelled"  # cancel_reason of an unpaid booking its booker cancelled (the WhatsApp path writes it too)
REASON_MAX_LENGTH = 500
DESTINATION = "original_method"  # the only destination the app is ever told about (the patient wallet is hidden in v1)

# What the family is shown for a refund: the internal state machine (spec section 4.3) is not theirs to see.
PATIENT_REFUND_STATUS = {
    "pending_approval": "under_review",
    "needs_review": "under_review",
    "approved": "processing",
    "processing": "processing",
    "failed": "processing",           # Finance retries a failed refund; the family only needs to know it is being handled
    "needs_attention": "needs_bank_details",
    "succeeded": "refunded",
    "rejected": "declined",
    "closed": "contact_support",
}


class PatientCancelError(Exception):
    """An error the endpoint turns into ``HTTPException(status_code, detail={"code", "message", **extra})``."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status_code, self.code, self.message, self.extra = status_code, code, message, extra


def _not_found() -> PatientCancelError:
    return PatientCancelError(404, "booking_not_found", "We could not find that booking.")


def _not_yours() -> PatientCancelError:
    return PatientCancelError(403, "not_your_booking", "Only the person who made this booking can cancel it.")


_STATE_ERRORS = {
    "booking_in_progress": (409, "The visit has already started, so it cannot be cancelled in the app. Please contact support."),
    "booking_completed": (409, "This visit is already completed, so it cannot be cancelled."),
    "booking_already_cancelled": (409, "This booking is already cancelled."),
    "booking_closed": (409, "This booking can no longer be cancelled."),
}


def _state_code(booking: Booking, refusal: Optional[str]) -> str:
    if booking.status == BookingStatus.CANCELLED:
        return "booking_already_cancelled"
    if booking.status == BookingStatus.IN_PROGRESS or refusal == cancellation.REASON_IN_PROGRESS:
        return "booking_in_progress"
    if booking.status == BookingStatus.COMPLETED:
        return "booking_completed"
    return "booking_closed"


def _state_error(code: str) -> PatientCancelError:
    status_code, message = _STATE_ERRORS[code]
    return PatientCancelError(status_code, code, message)


# --- who ---------------------------------------------------------------------------------------------------------
def _patient_of(db: Session, user: User) -> Optional[Patient]:
    return db.query(Patient).filter(Patient.user_id == user.id).first()


def is_booker(db: Session, booking: Booking, user: User) -> bool:
    """The booker: whoever made the booking (WhatsApp), else the patient it is for (app). Same rule as ``my_bookings``."""
    if booking.booked_by_user_id is not None:
        return booking.booked_by_user_id == user.id
    patient = _patient_of(db, user)
    return patient is not None and booking.patient_id == patient.id


def _load_for(db: Session, user: User, booking_id: uuid.UUID) -> Booking:
    booking = db.query(Booking).filter(Booking.id == booking_id).populate_existing().first()
    if booking is None:
        raise _not_found()
    if not is_booker(db, booking, user):
        raise _not_yours()
    return booking


# --- the refund summary ------------------------------------------------------------------------------------------
def refund_summaries(db: Session, booking_ids: Iterable[uuid.UUID]) -> Dict[uuid.UUID, Dict[str, Any]]:
    """``{booking_id: {"status", "amount"}}`` for the bookings that have a refund (the newest one when there are several).
    One query, so a list of bookings does not cost one per row."""
    ids = list(booking_ids)
    if not ids:
        return {}
    out: Dict[uuid.UUID, Dict[str, Any]] = {}
    rows: List[Refund] = db.query(Refund).filter(Refund.booking_id.in_(ids)).order_by(Refund.created_at.desc()).all()
    for refund in rows:
        if refund.booking_id in out:
            continue
        kobo = refund.amount_kobo if refund.amount_kobo is not None else refund.suggested_amount_kobo
        out[refund.booking_id] = {"status": PATIENT_REFUND_STATUS.get(refund.status, "under_review"),
                                  "amount": kobo_to_naira(kobo) if kobo is not None else None}
    return out


def cancel_fee_kobo(booking: Booking) -> Optional[int]:
    """What the family was charged for cancelling, or None when the booker did not cancel it. Derived from what the cancel
    stored (paid less the refund it set), so it never drifts from the refund."""
    if booking.cancel_reason == USER_CANCELLED:
        return 0
    if booking.cancel_reason != cancellation.CANCELLED_BY_BOOKER or booking.refund_amount_kobo is None:
        return None
    return max(0, int(booking.total_amount_kobo) - int(booking.refund_amount_kobo))


def attach_cancel_info(booking_dict: Dict[str, Any], booking: Booking, refunds: Optional[Dict[uuid.UUID, Dict[str, Any]]]) -> None:
    """Put ``cancel_fee_tier``, ``cancel_fee`` and ``refund`` on a booking dict for its own patient (never for caregivers)."""
    fee = cancel_fee_kobo(booking)
    booking_dict["cancel_fee_tier"] = booking.cancel_fee_tier if fee is not None else None
    booking_dict["cancel_fee"] = kobo_to_naira(fee) if fee is not None else None
    booking_dict["refund"] = (refunds or {}).get(booking.id)


# --- what it would cost (GET) -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class View:
    """The terms in the form the endpoint returns (naira are for display, kobo for echoing back as ``accepted_fee_kobo``)."""
    booking_id: uuid.UUID
    booking_code: str
    can_cancel: bool
    reason: Optional[str]
    message: Optional[str]
    fee_tier: str
    fee_percent: int
    fee_kobo: int
    paid_kobo: int
    refundable_kobo: int
    refund_will_be_queued: bool
    assigned: bool
    policy: Optional[Dict[str, int]]
    as_of: datetime

    def as_dict(self) -> Dict[str, Any]:
        return {
            "booking_id": self.booking_id, "booking_code": self.booking_code, "can_cancel": self.can_cancel,
            "reason": self.reason, "message": self.message, "fee_tier": self.fee_tier, "fee_percent": self.fee_percent,
            "fee_amount": kobo_to_naira(self.fee_kobo), "fee_amount_kobo": self.fee_kobo,
            "paid_amount": kobo_to_naira(self.paid_kobo), "paid_amount_kobo": self.paid_kobo,
            "refundable_amount": kobo_to_naira(self.refundable_kobo), "refundable_amount_kobo": self.refundable_kobo,
            "refund_will_be_queued": self.refund_will_be_queued,
            "refund_destination": DESTINATION if self.refund_will_be_queued else None,
            "refund_needs_approval": self.refund_will_be_queued,
            "caregiver_assigned": self.assigned, "policy": self.policy, "as_of": self.as_of,
        }


def _policy_dict(policy: Optional[cancellation.CancelPolicy]) -> Optional[Dict[str, int]]:
    if policy is None:
        return None
    return {"free_hours": policy.free_hours, "late_hours": policy.late_hours, "fee_percent": policy.fee_percent,
            "late_fee_percent": policy.late_fee_percent}


def _is_unpaid_pending(booking: Booking) -> bool:
    return booking.status == BookingStatus.PENDING and booking.payment_status in (PaymentStatus.PENDING, PaymentStatus.FAILED)


def _closed_view(booking: Booking, code: str, now: datetime) -> View:
    return View(booking.id, booking.booking_code, False, code, _STATE_ERRORS[code][1], cancellation.TIER_NONE, 0, 0,
                0, 0, False, booking.caregiver_id is not None, None, now)


def view_from_terms(db: Session, booking: Booking, terms: cancellation.Terms, now: datetime) -> View:
    # Never promise more than is left of what was paid (an earlier refund on the booking counts against it).
    refundable = min(terms.refund_kobo, refundable_kobo(db, booking))
    return View(booking.id, booking.booking_code, True, None, None, terms.tier, terms.fee_percent, terms.fee_kobo,
                int(booking.total_amount_kobo), refundable, refundable > 0, terms.assigned, _policy_dict(terms.policy), now)


def terms_view(db: Session, user: User, booking_id: uuid.UUID, now: Optional[datetime] = None) -> View:
    """What cancelling would cost right now. Read-only; the answer is only a preview (``cancel`` decides under the lock)."""
    now = now or datetime.utcnow()
    booking = _load_for(db, user, booking_id)
    if booking.status == BookingStatus.CANCELLED:
        return _closed_view(booking, "booking_already_cancelled", now)
    if _is_unpaid_pending(booking):
        # Not paid yet (the payment link may still be open): free, and nothing to refund. Cancelling first asks Paystack.
        policy = cancellation.policy_for(db, booking)
        return View(booking.id, booking.booking_code, True, None, None, cancellation.TIER_NONE, 0, 0, 0, 0, False, False,
                    _policy_dict(policy), now)
    decided = cancellation.terms_for(db, booking, now)
    if not decided.allowed:
        return _closed_view(booking, _state_code(booking, decided.refusal), now)
    return view_from_terms(db, booking, decided, now)


# --- the cancellation (POST) ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Result:
    booking: Booking
    view: View
    already_cancelled: bool
    refund: Optional[Dict[str, Any]]

    def as_dict(self) -> Dict[str, Any]:
        v = self.view
        return {
            "booking_id": v.booking_id, "booking_code": v.booking_code, "status": self.booking.status.value,
            "already_cancelled": self.already_cancelled, "fee_tier": v.fee_tier, "fee_percent": v.fee_percent,
            "fee_amount": kobo_to_naira(v.fee_kobo), "fee_amount_kobo": v.fee_kobo,
            "paid_amount": kobo_to_naira(v.paid_kobo), "paid_amount_kobo": v.paid_kobo,
            "refundable_amount": kobo_to_naira(v.refundable_kobo), "refundable_amount_kobo": v.refundable_kobo,
            "refund_queued": self.refund is not None,
            "refund_destination": DESTINATION if self.refund is not None else None,
            "refund_needs_approval": self.refund is not None, "caregiver_assigned": v.assigned, "refund": self.refund,
        }


def _replay(db: Session, booking: Booking) -> Result:
    """The booker's earlier cancel, as it was decided: the stored tier, fee and refund. Never recomputed from today's clock."""
    fee = cancel_fee_kobo(booking) or 0
    total = int(booking.total_amount_kobo)
    paid = booking.payment_status in PAID_STATES
    refund = refund_summaries(db, [booking.id]).get(booking.id)
    refundable = max(0, total - fee) if paid else 0
    view = View(booking.id, booking.booking_code, False, None, None, booking.cancel_fee_tier or cancellation.TIER_NONE,
                _percent_of(booking, fee), fee, total if paid else 0, refundable, refund is not None,
                booking.caregiver_id is not None, None, datetime.utcnow())
    return Result(booking, view, True, refund)


def _percent_of(booking: Booking, fee_kobo: int) -> int:
    service_fee = int(booking.service_fee_kobo)
    return round(fee_kobo * 100 / service_fee) if fee_kobo and service_fee else 0


def _by_booker(booking: Booking) -> bool:
    return booking.cancel_reason in (cancellation.CANCELLED_BY_BOOKER, USER_CANCELLED)


def _note_on_refund(db: Session, booking: Booking, reason: Optional[str]) -> None:
    """The family's reason travels with the refund the approver sees (there is no booking column for it)."""
    text = (reason or "").strip()[:REASON_MAX_LENGTH]
    if not text:
        return
    refund = db.query(Refund).filter(Refund.booking_id == booking.id).order_by(Refund.created_at.desc()).first()
    if refund is not None and not refund.note:
        refund.note = f"Reason given by the family: {text}"


def cancel(db: Session, user: User, booking_id: uuid.UUID, *, reason: Optional[str] = None,
           accepted_fee_kobo: Optional[int] = None, now: Optional[datetime] = None) -> Result:
    """Cancel the booking for its booker. Commits (or rolls back and raises); see the module docstring for the rules."""
    now = now or datetime.utcnow()
    unpaid = _is_unpaid_pending(_load_for(db, user, booking_id))
    db.rollback()  # read-only so far: end the transaction before anything slow (Paystack) or locking
    try:
        if unpaid:
            outcome = _cancel_unpaid(db, booking_id, now)
            if outcome is not None:
                return outcome
        return _cancel_paid(db, booking_id, reason, accepted_fee_kobo, now)
    except BaseException:
        db.rollback()
        raise


def _cancel_unpaid(db: Session, booking_id: uuid.UUID, now: datetime) -> Optional[Result]:
    """Verify-before-cancel. Returns the result, or None when the booking turned out to be paid (the caller then cancels it
    as a paid booking). Only the booking row is ever locked, and never across the Paystack call."""
    from app.services.booking_payment import SettleOutcome, verify_and_settle

    settled = verify_and_settle(db, booking_id)
    if settled == SettleOutcome.UNREACHABLE:
        # Not knowing is not the same as "not paid": cancelling now could cancel a paid booking.
        raise PatientCancelError(503, "payment_check_unavailable",
                                 "We could not check your payment with our payment provider. Please try again in a moment.")
    if settled == SettleOutcome.AMOUNT_MISMATCH:
        raise PatientCancelError(409, "payment_under_review",
                                 "Your payment needs a manual check by our team, so the booking cannot be cancelled here. "
                                 "Please contact support.")
    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().populate_existing().first()
    if booking.payment_status in PAID_STATES:
        db.rollback()  # a webhook (or the check above) settled it: it is a paid booking now
        return None
    if booking.status == BookingStatus.CANCELLED:
        db.rollback()
        return _already(db, booking)
    if booking.status != BookingStatus.PENDING:
        db.rollback()
        raise _state_error(_state_code(booking, None))
    booking.status = BookingStatus.CANCELLED
    booking.cancel_reason = USER_CANCELLED
    booking.cancel_fee_tier = cancellation.TIER_NONE
    NotificationService(db).cancelled_by_booker(booking)  # nobody is assigned before payment: a no-op kept for symmetry
    db.commit()
    logger.info("booking %s cancelled by its booker before payment", booking.booking_code)
    result = _replay(db, booking)
    return Result(result.booking, result.view, False, None)


def _already(db: Session, booking: Booking) -> Result:
    if _by_booker(booking):
        return _replay(db, booking)
    raise _state_error("booking_already_cancelled")


def _cancel_paid(db: Session, booking_id: uuid.UUID, reason: Optional[str], accepted_fee_kobo: Optional[int],
                 now: datetime) -> Result:
    outcome = cancellation.cancel_by_booker(db, booking_id, now, commit=False, accepted_fee_kobo=accepted_fee_kobo)
    booking = db.query(Booking).filter(Booking.id == booking_id).first()  # the row we hold the lock on
    if booking is None:
        db.rollback()
        raise _not_found()
    if outcome.status == "already_cancelled":
        db.rollback()
        return _already(db, booking)
    if outcome.status == "terms_changed" and outcome.terms is not None:
        view = view_from_terms(db, booking, outcome.terms, now)
        db.rollback()
        raise PatientCancelError(
            409, "terms_changed", "The cancellation fee has changed since you last looked. Please check it again.",
            terms=_jsonable(view.as_dict()))
    if outcome.status == "refused":
        refusal = outcome.terms.refusal if outcome.terms is not None else None
        code = _state_code(booking, refusal)
        db.rollback()
        raise _state_error(code)
    terms = outcome.terms
    assert terms is not None and outcome.status == "cancelled"
    _note_on_refund(db, booking, reason)
    db.commit()
    db.refresh(booking)
    refund = refund_summaries(db, [booking.id]).get(booking.id)
    return Result(booking, view_from_terms(db, booking, terms, now), False, refund)


def _jsonable(data: Dict[str, Any]) -> Dict[str, Any]:
    """An error ``detail`` is serialised by Starlette without FastAPI's encoder: dates and ids must already be strings."""
    def plain(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, uuid.UUID):
            return str(v)
        return float(v) if isinstance(v, Decimal) else v

    return {k: plain(v) for k, v in data.items()}
