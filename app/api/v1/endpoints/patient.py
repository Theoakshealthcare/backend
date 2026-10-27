from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
import csv
from io import StringIO
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_permission
from app.db.models.admin import AdminUser
from app.db.models.user import User
from app.db.models.patient import Patient
from app.schemas.patient import (
    PatientWalletSummary,
    PatientTransactionOut,
    PatientDepositRequest,
    PatientDepositResponse,
    PatientPaymentRequest,
    PatientPaymentResponse,
    PatientRefundRequestOut,
    PaymentVerificationRequest,
    PaymentVerificationResponse
)
from app.services.patient_wallet import PatientWalletService
from app.services.paystack import PaystackService


router = APIRouter()


def get_patient_wallet_service(db: Session = Depends(get_db)) -> PatientWalletService:
    return PatientWalletService(db)


def get_paystack_service() -> PaystackService:
    return PaystackService()


@router.get("/wallet", response_model=PatientWalletSummary)
def get_wallet_summary(
    current_user: User = Depends(get_current_user),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    db: Session = Depends(get_db),
):
    """Get patient wallet summary"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    
    patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not patient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient profile not found."
        )
    
    return wallet_service.get_wallet_summary(patient.id)


@router.post("/wallet/deposit", response_model=PatientDepositResponse)
def initiate_deposit(
    deposit_request: PatientDepositRequest,
    current_user: User = Depends(get_current_user),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    db: Session = Depends(get_db),
):
    """Initiate a wallet deposit via Paystack"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    
    patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not patient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient profile not found."
        )
    
    try:
        from app.core.config import settings
        callback_url = settings.PAYMENT_CALLBACK_URL
        response = wallet_service.initiate_deposit(
            patient.id, 
            deposit_request,
            callback_url
        )
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/wallet/payment", response_model=PatientPaymentResponse)
def make_payment(
    payment_request: PatientPaymentRequest,
    current_user: User = Depends(get_current_user),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    db: Session = Depends(get_db),
):
    """Make a payment from wallet balance"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    
    patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not patient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient profile not found."
        )
    
    return wallet_service.make_payment(patient.id, payment_request)


@router.post("/wallet/verify-payment", response_model=PaymentVerificationResponse)
def verify_payment(
    verification_request: PaymentVerificationRequest,
    current_user: User = Depends(get_current_user),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    db: Session = Depends(get_db),
):
    """Verify payment with Paystack"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    
    patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient profile not found.")

    try:
        return wallet_service.verify_payment(verification_request.reference, patient.id)
    except LookupError:
        # same response for unknown and foreign references
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")


@router.get("/wallet/transactions", response_model=List[PatientTransactionOut])
def get_wallet_transactions(
    month: Optional[str] = Query(None, description="Filter by month in YYYY-MM format"),
    transaction_type: Optional[str] = Query(None, description="Filter by type: deposit, payment, refund"),
    current_user: User = Depends(get_current_user),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    db: Session = Depends(get_db),
):
    """Get patient wallet transaction history"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    
    patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not patient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient profile not found."
        )
    
    # Validate transaction type if provided
    if transaction_type and transaction_type not in ["deposit", "payment", "refund"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid transaction type. Must be: deposit, payment, or refund"
        )
    
    try:
        return wallet_service.get_transactions(patient.id, month, transaction_type)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/wallet/statement")
def download_wallet_statement(
    month: str = Query(..., description="Statement month in YYYY-MM format"),
    current_user: User = Depends(get_current_user),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    db: Session = Depends(get_db),
):
    """Download wallet statement as CSV"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    
    patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not patient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient profile not found."
        )
    
    try:
        # Validate month format
        datetime.strptime(month, "%Y-%m")
        
        transactions = wallet_service.get_transactions(patient.id, month)
        
        # Generate CSV
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Date", "Type", "Amount (NGN)", "Status", 
            "Payment Method", "Reference", "Description"
        ])
        
        for t in transactions:
            writer.writerow([
                t.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                t.type.title(),
                f"{t.amount:,.2f}",
                t.status.title(),
                t.payment_method or "N/A",
                t.paystack_reference or "N/A",
                t.description or "N/A"
            ])
        
        output.seek(0)
        filename = f"wallet_statement_{month}.csv"
        
        return StreamingResponse(
            output, 
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid month format. Use YYYY-MM."
        )


PATIENT_REFUND_GONE = "Refunds are now handled by the Oakly team. Contact support to request one."
ADMIN_REFUND_MOVED = "This route has moved to /api/v1/admin/refunds."


@router.post("/wallet/refund/request", status_code=status.HTTP_410_GONE)
def request_refund(current_user: User = Depends(get_current_user)):
    """Retired (Refunds v2): a refund is raised by Oakly staff or by a cancellation, never by the patient."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=PATIENT_REFUND_GONE)


@router.get("/wallet/refund/requests", response_model=List[PatientRefundRequestOut])
def get_refund_requests(
    current_user: User = Depends(get_current_user),
    wallet_service: PatientWalletService = Depends(get_patient_wallet_service),
    db: Session = Depends(get_db),
):
    """Get patient's refund requests"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    
    patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
    if not patient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient profile not found."
        )
    
    requests = wallet_service.get_refund_requests(patient.id)
    
    return [
        PatientRefundRequestOut(
            id=req["id"],
            transaction_id=req["transaction_id"],
            amount=req["amount"],
            reason=req["reason"],
            status=req["status"],
            admin_notes=req["admin_notes"],
            created_at=req["created_at"],
            processed_at=req["processed_at"]
        )
        for req in requests
    ]


# Admin endpoints for refund management: retired, they moved to /admin/refunds (the permission check stays, so a caller
# without a token still gets 401 and without the permission 403).
@router.get("/admin/refund/requests", status_code=status.HTTP_410_GONE)
def get_all_refund_requests(current_user: AdminUser = Depends(require_permission("refunds.approve"))):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=ADMIN_REFUND_MOVED)


@router.put("/admin/refund/requests/{request_id}", status_code=status.HTTP_410_GONE)
def process_refund_request(request_id: UUID, current_user: AdminUser = Depends(require_permission("refunds.approve"))):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=ADMIN_REFUND_MOVED)
