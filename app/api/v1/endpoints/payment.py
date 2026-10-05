import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.money import to_kobo, to_kobo_or_none
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.prescription import Prescription
from app.db.session import SessionLocal
from app.services.booking_payment import settle_booking_payment
from app.services.patient_wallet import PatientWalletService
from app.services.paystack import PaystackService

router = APIRouter()

logger = logging.getLogger(__name__)


def get_paystack_service() -> PaystackService:
    return PaystackService()


def _paid_kobo(payload: Dict[str, Any]):
    amount = payload.get("data", {}).get("amount")
    return amount if isinstance(amount, int) else to_kobo_or_none(amount)


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
                outcome = settle_booking_payment(db, booking.id, _paid_kobo(payload))
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
                paid = _paid_kobo(payload)
                if prescription.total_cost is not None and paid == to_kobo(prescription.total_cost):
                    prescription.payment_status = "paid"
                    db.commit()
                    logger.info(f"Prescription payment processed: {reference}")
                else:
                    db.rollback()
                    logger.error(
                        f"Prescription {reference} amount mismatch: expected "
                        f"{prescription.total_cost}, paystack reported {paid} kobo"
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
            if booking and booking.payment_status != PaymentStatus.PAID:
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

        elif event in ("refund_processed", "refund_failed"):
            wallet_service.process_refund_webhook(event_data.get("refund_id"), event == "refund_processed")

        elif event == "refund_successful":
            logger.info(f"Refund successful for reference: {reference}")
    finally:
        db.close()


@router.post("/paystack/webhook")
async def paystack_webhook(
    request: Request,
    paystack_service: PaystackService = Depends(get_paystack_service),
):
    """Paystack webhook for bookings, prescriptions, wallet deposits and refunds."""

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
