from fastapi import APIRouter, Request, HTTPException, status, Depends
from sqlalchemy.orm import Session
from app.db.models.booking import Booking, PaymentStatus
from app.db.models.prescription import Prescription
from app.api.deps import get_db
import hmac
import hashlib
import os

router = APIRouter()

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")

@router.post("/paystack/webhook")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)):
    # Verify Paystack signature
    signature = request.headers.get("x-paystack-signature")
    body = await request.body()
    if PAYSTACK_SECRET_KEY:
        expected_signature = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
        if signature != expected_signature:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")
    data = await request.json()
    event = data.get("event")
    if event == "charge.success":
        reference = data["data"]["reference"]
        # Find booking by paystack_reference
        booking = db.query(Booking).filter(Booking.paystack_reference == reference).first()
        if booking:
            booking.payment_status = PaymentStatus.PAID
            db.commit()
        # Find prescription by paystack_reference
        prescription = db.query(Prescription).filter(Prescription.paystack_reference == reference).first()
        if prescription:
            prescription.payment_status = "paid"
            db.commit()
    return {"status": True} 