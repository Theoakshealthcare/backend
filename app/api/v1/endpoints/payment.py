from fastapi import APIRouter, Request, HTTPException, status, Depends
from sqlalchemy.orm import Session
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.prescription import Prescription
from app.api.deps import get_db
from app.services.patient_wallet import PatientWalletService
from app.services.paystack import PaystackService
from app.services.booking import BookingService
import logging

router = APIRouter()

# Set up logging
logger = logging.getLogger(__name__)

def get_patient_wallet_service(db: Session = Depends(get_db)) -> PatientWalletService:
    return PatientWalletService(db)

def get_paystack_service() -> PaystackService:
    return PaystackService()

@router.post("/paystack/webhook")
async def paystack_webhook(
    request: Request, 
    db: Session = Depends(get_db),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    paystack_service: PaystackService = Depends(get_paystack_service),
):
    """Enhanced Paystack webhook handler for bookings, prescriptions, and wallet transactions"""
    
    # Get raw body and signature
    body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")
    
    # Verify webhook signature
    if not paystack_service.verify_webhook_signature(body, signature):
        logger.warning(f"Invalid webhook signature received: {signature}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature"
        )
    
    try:
        # Parse webhook payload
        payload = await request.json()
        event_data = paystack_service.parse_webhook_event(payload)
        reference = event_data.get("reference", "")
        
        logger.info(f"Processing webhook event: {event_data.get('event')} for reference: {reference}")
        
        # Handle successful payments
        if event_data["event"] == "payment_successful":
            # Check if it's a wallet transaction (DEP_ prefix)
            if reference.startswith("DEP_"):
                success = wallet_service.process_successful_payment(reference, event_data)
                if success:
                    logger.info(f"Wallet deposit processed successfully: {reference}")
                else:
                    logger.error(f"Failed to process wallet deposit: {reference}")
            else:
                # Handle booking payments
                booking = db.query(Booking).filter(Booking.paystack_reference == reference).first()
                if booking:
                    booking.payment_status = PaymentStatus.PAID
                    booking.status = BookingStatus.PENDING  # Set to pending after payment
                    db.commit()
                    
                    # Now that payment is successful, notify nearby caregivers
                    booking_service = BookingService(db)
                    notifications = booking_service.notify_nearby_caregivers(booking)
                    logger.info(f"Booking payment processed: {reference}, notified {len(notifications)} caregivers")
                
                # Handle prescription payments
                prescription = db.query(Prescription).filter(Prescription.paystack_reference == reference).first()
                if prescription:
                    prescription.payment_status = "paid"
                    db.commit()
                    logger.info(f"Prescription payment processed: {reference}")
        
        # Handle failed payments
        elif event_data["event"] == "payment_failed":
            # Check if it's a wallet transaction
            if reference.startswith("DEP_"):
                success = wallet_service.process_failed_payment(reference, event_data)
                if success:
                    logger.info(f"Wallet deposit failure processed: {reference}")
                else:
                    logger.error(f"Failed to process wallet deposit failure: {reference}")
            else:
                # Handle booking payment failures
                booking = db.query(Booking).filter(Booking.paystack_reference == reference).first()
                if booking:
                    booking.payment_status = PaymentStatus.FAILED
                    booking.status = BookingStatus.CANCELLED  # Cancel booking on payment failure
                    # Store failure reason
                    booking.payment_failure_reason = event_data.get("gateway_response", "Payment failed")
                    db.commit()
                    logger.info(f"Booking payment failed: {reference}, reason: {booking.payment_failure_reason}")
                
                # Handle prescription payment failures
                prescription = db.query(Prescription).filter(Prescription.paystack_reference == reference).first()
                if prescription:
                    prescription.payment_status = "failed"
                    db.commit()
                    logger.info(f"Prescription payment failed: {reference}")
        
        # Handle successful refunds
        elif event_data["event"] == "refund_successful":
            logger.info(f"Refund successful for reference: {reference}")
            # Additional refund processing logic can be added here
        
        return {"status": "success", "message": "Webhook processed successfully"}
        
    except Exception as e:
        logger.error(f"Webhook processing error for reference {reference}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed"
        ) 