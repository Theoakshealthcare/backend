import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.db.models.booking import PAID_STATES, Booking, BookingStatus, PaymentStatus
from app.db.models.prescription import Prescription
from app.db.session import SessionLocal
from app.services.booking_payment import settle_booking_payment
from app.services import payouts, refunds
from app.services.patient_wallet import PatientWalletService
from app.services.paystack import PaystackService

router = APIRouter()

logger = logging.getLogger(__name__)


TRANSFER_EVENTS = {"transfer_success": "success", "transfer_failed": "failed", "transfer_reversed": "reversed"}


def get_paystack_service() -> PaystackService:
    return PaystackService()


def _paid_kobo(event_data: Dict[str, Any]):
    """Kobo Paystack says was paid; anything that is not a plain integer is treated as unknown."""
    amount = event_data.get("amount_kobo")
    return amount if isinstance(amount, int) and not isinstance(amount, bool) else None


def _process_event(payload: Dict[str, Any], event_data: Dict[str, Any], paystack_service: PaystackService) -> None:
    """All blocking DB work for a verified webhook. Runs in a worker thread with its own
    session, so row locks never stall the event loop."""
    db: Session = SessionLocal()
    try:
        wallet_service = PatientWalletService(db)
        reference = event_data.get("reference") or ""
        event = event_data.get("event")

        if event == "payment_successful":
            if reference.startswith("DEP_"):
                if wallet_service.process_successful_payment(reference, event_data):
                    logger.info(f"Wallet deposit processed successfully: {reference}")
                else:
                    logger.error(f"Failed to process wallet deposit: {reference}")
                return

            booking = db.query(Booking).filter(Booking.paystack_reference == reference).first()
            if booking:
                outcome = settle_booking_payment(db, booking.id, _paid_kobo(event_data),
                                                fees_kobo=event_data.get("fees_kobo"))
                logger.info(f"Booking payment {reference}: {outcome.value}")
            db.rollback()

            prescription = (
                db.query(Prescription)
                .filter(Prescription.paystack_reference == reference)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if prescription and prescription.payment_status != "paid":
                paid = _paid_kobo(event_data)
                if prescription.total_cost_kobo is not None and paid == prescription.total_cost_kobo:
                    prescription.payment_status = "paid"
                    db.commit()
                    logger.info(f"Prescription payment processed: {reference}")
                else:
                    db.rollback()
                    logger.error(
                        f"Prescription {reference} amount mismatch: expected "
                        f"{prescription.total_cost_kobo} kobo, paystack reported {paid} kobo"
                    )
            else:
                db.rollback()

        elif event == "payment_failed":
            if reference.startswith("DEP_"):
                if wallet_service.process_failed_payment(reference, event_data):
                    logger.info(f"Wallet deposit failure processed: {reference}")
                else:
                    logger.error(f"Failed to process wallet deposit failure: {reference}")
                return

            booking = (
                db.query(Booking)
                .filter(Booking.paystack_reference == reference)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if booking and booking.payment_status not in PAID_STATES:
                booking.payment_status = PaymentStatus.FAILED
                booking.status = BookingStatus.CANCELLED
                booking.payment_failure_reason = event_data.get("gateway_response", "Payment failed")
                db.commit()
                logger.info(f"Booking payment failed: {reference}, reason: {booking.payment_failure_reason}")
            else:
                if booking:
                    logger.warning(f"Ignoring charge.failed for already-paid booking: {reference}")
                db.rollback()

            prescription = (
                db.query(Prescription)
                .filter(Prescription.paystack_reference == reference)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if prescription and prescription.payment_status != "paid":
                prescription.payment_status = "failed"
                db.commit()
                logger.info(f"Prescription payment failed: {reference}")
            else:
                db.rollback()

        elif event == "refund_event":
            # A refund Oakly sent (Refunds v2). Idempotent: Paystack retries, and reconcile may settle the same one. A
            # refund that is not ours (no open refund on that booking) is logged and ignored.
            result = refunds.apply_refund_event(
                db, status=event_data["refund_status"], transaction_reference=event_data.get("reference"),
                paystack_refund_id=event_data.get("refund_id"), amount_kobo=event_data.get("amount_kobo"),
                reason=event_data.get("reason"))
            logger.info(f"Refund {event_data['refund_status']}: {event_data.get('reference')} -> {result}")

        elif event == "refund_successful":
            logger.info(f"Refund successful for reference: {reference}")

        elif event in TRANSFER_EVENTS:
            # A caregiver payout settled by Paystack. Idempotent: a retried webhook changes nothing. Paystack sends these
            # for every transfer on the account, so an unknown reference is normal and ignored.
            outcome = TRANSFER_EVENTS[event]
            result = payouts.apply_transfer_event(
                db, reference, outcome, amount_kobo=_paid_kobo(event_data), transfer_code=event_data.get("transfer_code"),
                reason=None if outcome == "success" else f"Paystack reported the transfer as {outcome}")
            logger.info(f"Payout transfer {outcome}: {reference} -> {result}")
    finally:
        db.close()


@router.post("/paystack/webhook")
async def paystack_webhook(
    request: Request,
    paystack_service: PaystackService = Depends(get_paystack_service),
):
    """Paystack webhook for bookings, prescriptions, wallet deposits, refunds and caregiver payout transfers."""

    # Signature is verified over the raw body, before anything is parsed.
    body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    if not paystack_service.secret_key:
        logger.error("Webhook rejected: PAYSTACK_SECRET_KEY is not configured")
    if not paystack_service.verify_webhook_signature(body, signature):
        logger.warning("Invalid webhook signature received")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature"
        )

    reference = ""
    try:
        payload = json.loads(body)
        event_data = paystack_service.parse_webhook_event(payload)
        reference = event_data.get("reference") or ""
        logger.info(f"Processing webhook event: {event_data.get('event')} for reference: {reference}")

        await run_in_threadpool(_process_event, payload, event_data, paystack_service)

        return {"status": "success", "message": "Webhook processed successfully"}

    except Exception as e:
        logger.error(f"Webhook processing error for reference {reference}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed"
        )
