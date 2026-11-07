"""A fresh "Pay now" link for a booking that was created but not paid (so the link survives an app restart).

``/booking/create`` is the only place a payment link used to be handed out. This is the same Paystack initialisation
(``BookingService.initialise_payment``: the booking's own reference, total and metadata) for the booker, any time while the
booking is unpaid and its payment window (``payment_expires_at``) is open.

Why it cannot double charge or create a second transaction:

* **One reference per booking.** The booking keeps the reference it was created with, so every link for it is the same
  Paystack transaction, and Paystack accepts a successful charge on a reference once. Webhook, verify-payment, the expiry
  sweep and refunds all keep finding the booking by that reference, so nothing here needs them to change.
  KNOWN LIMIT: Paystack's error reference says a reference already used on the integration cannot be initialised again
  ("Duplicate Transaction Reference"), so against the real Paystack this endpoint is expected to answer 503
  ``payment_link_unavailable`` for any booking that ``/create`` already initialised. The working fix is to keep the
  ``authorization_url`` ``/create`` got (a nullable column, so a migration) and hand it back here; see the SOP.
* **Nothing is written** when a link is handed out (no row, no state change), so a repeat or a retry cannot leave a
  second record behind.
* **Paystack is asked first.** A payment whose webhook is late is settled (the same ``settle_booking_payment`` the webhook
  and verify-payment use) and the person is told it is paid, instead of being given a link to pay again. If Paystack cannot
  be asked, nothing is issued (503): not knowing is not "unpaid".
* **One request at a time per booking.** A transaction-scoped advisory lock (not the booking row lock, which the webhook
  needs) is held while Paystack is called; a second request while the first is running gets 409
  ``payment_link_in_progress``.

A failed initialisation never cancels the booking here (``/create`` does, because nothing was ever payable): the person
may still hold a working link, so the booking is left exactly as it was and the answer is 503 ``payment_link_unavailable``.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.db.models.booking import PAID_STATES, Booking, BookingStatus, PaymentStatus
from app.db.models.user import User
from app.services.booking import BookingService
from app.services.booking_payment import SettleOutcome, settle_booking_payment
from app.services.patient_booking_cancel import PatientCancelError, is_booker
from app.services.paystack import PaystackService

logger = logging.getLogger(__name__)



def _lock_key(booking_id: uuid.UUID) -> int:
    """63 bits of the booking id (positive bigint): two bookings colliding, and so a false "in progress", is negligible.
    The other advisory locks in the app are small constants (booking settings 7_204_001, KYC 7_204_1xx)."""
    return int.from_bytes(booking_id.bytes[:8], "big") >> 1


def _error(status_code: int, code: str, message: str, **extra: Any) -> PatientCancelError:
    return PatientCancelError(status_code, code, message, **extra)


def payment_window_closes_at(booking: Booking) -> datetime:
    """When the unpaid booking stops being payable. Rows from before ``payment_expires_at`` existed use created_at + TTL,
    exactly as the expiry sweep does."""
    return booking.payment_expires_at or (booking.created_at + timedelta(minutes=app_settings.PAYMENT_LINK_TTL_MINUTES))


def _callback_url(booking: Booking) -> str:
    """Where Paystack sends the person after paying: the same URL the booking's own channel used when it was created."""
    if booking.channel == "whatsapp":
        return f"{app_settings.WHATSAPP_PAYMENT_RETURN_URL}?ref={booking.booking_code}"
    return f"{app_settings.FRONTEND_URL}/bookings/{booking.id}/payment-complete"


def _paid_error(booking: Booking) -> PatientCancelError:
    return _error(409, "booking_already_paid", "This booking is already paid.",
                  payment_status=booking.payment_status.value, refund_required=bool(booking.refund_required))


def _refuse_unpayable(booking: Booking) -> None:
    if booking.payment_status in PAID_STATES:
        raise _paid_error(booking)
    if booking.status in (BookingStatus.CANCELLED, BookingStatus.DECLINED):
        raise _error(409, "booking_already_cancelled", "This booking is cancelled, so it can no longer be paid.")
    if (booking.status != BookingStatus.PENDING or booking.payment_status != PaymentStatus.PENDING
            or not booking.paystack_reference):
        raise _error(409, "booking_not_payable", "This booking cannot be paid in the app.")


def issue(db: Session, user: User, booking_id: uuid.UUID, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Returns ``{"booking", "payment_url", "access_code"}`` or raises ``PatientCancelError`` (the endpoint's error type).
    Always ends its transaction, which releases the advisory lock."""
    now = now or datetime.utcnow()
    try:
        result = _issue(db, user, booking_id, now)
    finally:
        db.rollback()  # no-op after a commit; releases the advisory lock on every other path
    db.refresh(result["booking"])  # the rollback expired it
    return result


def _issue(db: Session, user: User, booking_id: uuid.UUID, now: datetime) -> Dict[str, Any]:
    booking = db.query(Booking).filter(Booking.id == booking_id).populate_existing().first()
    if booking is None:
        raise _error(404, "booking_not_found", "We could not find that booking.")
    if not is_booker(db, booking, user):
        raise _error(403, "not_your_booking", "Only the person who made this booking can pay for it.")
    _refuse_unpayable(booking)  # cheap refusals first: no lock, no Paystack call

    got = db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _lock_key(booking_id)}).scalar()
    if not got:
        raise _error(409, "payment_link_in_progress",
                     "We are already preparing your payment link. Please try again in a moment.")
    db.refresh(booking)  # re-read under the lock: it may have been paid or cancelled while we waited
    _refuse_unpayable(booking)

    # Paid but the webhook is late? Ask Paystack before offering to pay again (same check as verify-payment).
    reference = booking.paystack_reference
    verified = PaystackService().verify_transaction(reference)
    if verified.get("unreachable"):
        logger.warning("Booking %s: payment link not issued, Paystack unreachable for the paid check", booking_id)
        raise _error(503, "payment_check_unavailable",
                     "We could not check your payment with our payment provider. Please try again in a moment.")
    data = verified.get("data") or {}
    if verified.get("status") and data.get("status") == "success":
        _settle_and_refuse(db, booking, data)

    if payment_window_closes_at(booking) <= now:  # after the paid check: a payment made in time still counts
        raise _error(409, "payment_window_expired",
                     "The time to pay for this booking has run out. Please make a new booking.")

    payment = BookingService(db).initialise_payment(booking, callback_url=_callback_url(booking))
    if not payment.get("status"):
        logger.warning("Booking %s: Paystack would not initialise a payment link: %s", booking_id, payment.get("message"))
        raise _error(503, "payment_link_unavailable",
                     "We could not prepare your payment link right now. Please try again in a moment.")
    return {"booking": booking, "payment_url": payment.get("authorization_url"), "access_code": payment.get("access_code")}


def _settle_and_refuse(db: Session, booking: Booking, data: Dict[str, Any]) -> None:
    """Paystack says it was paid: settle it the way the webhook would, then refuse (there is nothing left to pay). Always raises."""
    booking_id = booking.id
    outcome = settle_booking_payment(db, booking_id, data.get("amount_kobo"), fees_kobo=data.get("fees_kobo"))
    logger.info("Booking %s was already paid when a payment link was asked for: %s", booking_id, outcome.value)
    if outcome == SettleOutcome.AMOUNT_MISMATCH:
        raise _error(409, "payment_under_review",
                     "Your payment needs a manual check by our team. Please contact support.")
    db.expire_all()
    fresh = db.query(Booking).filter(Booking.id == booking_id).populate_existing().first()
    raise _paid_error(fresh if fresh is not None else booking)
