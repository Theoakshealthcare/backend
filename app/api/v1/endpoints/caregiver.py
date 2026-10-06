from datetime import timedelta
from typing import Any, Dict, List, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, StreamingResponse
import csv
from io import StringIO
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.api.deps import get_current_user, get_db, get_user_or_admin, is_admin
from app.core.config import settings
from app.db.models.admin import AdminUser
from app.db.models.user import User
from app.schemas.caregiver import (
    CaregiverProfile,
    CaregiverProfileStep1,
    CaregiverProfileStep2,
    CaregiverConsentData,
    DocumentUploadResponse,
    IdentityVerificationResponse,
    SpecializationOptions,
    VerificationResultRequest,
    CaregiverWalletSummary,
    CaregiverTransactionOut,
    CaregiverBankAccountOut,
    CaregiverBankAccountUpdate,
    CaregiverWithdrawalRequest,
    CaregiverWithdrawalResponse
)
from app.services.caregiver import CaregiverService
from app.db.models.caregiver import CaregiverWallet, CaregiverTransaction, CaregiverBankAccount, Caregiver, CaregiverDocument

router = APIRouter()

# Document types that may be rendered inline by the download route.
INLINE_DOCUMENT_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})


def get_caregiver_service(db: Session = Depends(get_db)) -> CaregiverService:
    return CaregiverService(db)


@router.get("/profile", response_model=CaregiverProfile)
def get_caregiver_profile(
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Get caregiver profile"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    caregiver = caregiver_service.get_caregiver_by_user_id(current_user.id)
    if not caregiver:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caregiver profile not found"
        )
    
    return caregiver


@router.put("/profile/step1", response_model=CaregiverProfile)
def update_profile_step1(
    profile_data: CaregiverProfileStep1,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Update caregiver profile - Step 1 (Personal Information)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        caregiver = caregiver_service.update_profile_step1(current_user.id, profile_data)
        return caregiver
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.put("/profile/step2", response_model=CaregiverProfile)
def update_profile_step2(
    profile_data: CaregiverProfileStep2,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Update caregiver profile - Step 2 (Professional Information)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        caregiver = caregiver_service.update_profile_step2(current_user.id, profile_data)
        return caregiver
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/documents/upload", response_model=DocumentUploadResponse)
def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(...),
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Upload caregiver document (license, certification, etc.)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    # Validate file type
    allowed_types = ["application/pdf", "image/jpeg", "image/png", "image/jpg"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Only PDF and image files are allowed."
        )
    
    # Validate file size (10MB max)
    if file.size > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size too large. Maximum 10MB allowed."
        )
    
    try:
        document = caregiver_service.upload_document(current_user.id, file, document_type)
        return document
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/documents/{document_id}/file")
def download_document(
    document_id: UUID,
    current_user: Union[User, AdminUser] = Depends(get_user_or_admin("documents.read")),
    db: Session = Depends(get_db),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Stream a caregiver document. Only the owning caregiver or an admin may read it."""
    document = db.query(CaregiverDocument).filter(CaregiverDocument.id == document_id).first()
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if not is_admin(current_user):
        owner = caregiver_service.get_caregiver_by_user_id(current_user.id)
        if current_user.user_type != "caregiver" or not owner or owner.id != document.caregiver_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    path = caregiver_service.resolve_document_path(document.file_path)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document file not found")

    # Never trust the stored MIME type (legacy rows predate upload validation): only
    # allowlisted types render inline; anything else is forced to download. The sandbox
    # CSP stops an inline document from running script in the API's origin.
    if document.mime_type in INLINE_DOCUMENT_TYPES:
        media_type, disposition = document.mime_type, "inline"
    else:
        media_type, disposition = "application/octet-stream", "attachment"
    return FileResponse(
        path,
        media_type=media_type,
        filename=document.document_name,
        content_disposition_type=disposition,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
        },
    )


@router.put("/consent", response_model=CaregiverProfile)
def update_consent(
    consent_data: CaregiverConsentData,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Update caregiver consent preferences"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        caregiver = caregiver_service.update_consent(current_user.id, consent_data)
        return caregiver
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/verification-status", response_model=dict)
def get_verification_status(
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Get current verification status"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    
    try:
        return caregiver_service.get_verification_status(current_user.id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/form-options", response_model=dict)
def get_form_options() -> Any:
    """Get form options for dropdowns (specializations, service areas, etc.)"""
    return CaregiverService.get_form_options()


@router.post("/identity-verification/result", response_model=dict)
def receive_verification_result(
    payload: VerificationResultRequest,
    current_user: User = Depends(get_current_user),
    caregiver_service: CaregiverService = Depends(get_caregiver_service),
) -> Any:
    """Receive and store verification result from mobile app (Prembly)"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    try:
        caregiver_service.process_verification_result(current_user.id, payload.type, payload.result)
        return {"message": "Verification result received and stored."}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/wallet", response_model=CaregiverWalletSummary)
def get_wallet_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if not caregiver:
        raise HTTPException(status_code=404, detail="Caregiver profile not found.")
    wallet = db.query(CaregiverWallet).filter(CaregiverWallet.caregiver_id == caregiver.id).first()
    if not wallet:
        wallet = CaregiverWallet(caregiver_id=caregiver.id, balance=0)
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    total_earned = db.query(CaregiverTransaction).filter(
        CaregiverTransaction.caregiver_id == caregiver.id,
        CaregiverTransaction.type == 'earning',
        CaregiverTransaction.status == 'successful'
    ).with_entities(func.coalesce(func.sum(CaregiverTransaction.amount), 0)).scalar()
    total_withdrawn = db.query(CaregiverTransaction).filter(
        CaregiverTransaction.caregiver_id == caregiver.id,
        CaregiverTransaction.type == 'withdrawal',
        CaregiverTransaction.status == 'successful'
    ).with_entities(func.coalesce(func.sum(CaregiverTransaction.amount), 0)).scalar()
    return CaregiverWalletSummary(
        balance=wallet.balance,
        total_earned=total_earned or 0,
        total_withdrawn=total_withdrawn or 0,
        currency="NGN"
    )


@router.get("/wallet/transactions", response_model=List[CaregiverTransactionOut])
def get_wallet_transactions(
    month: str = Query(None, description="Filter by month in YYYY-MM format"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if not caregiver:
        raise HTTPException(status_code=404, detail="Caregiver profile not found.")
    query = db.query(CaregiverTransaction).filter(CaregiverTransaction.caregiver_id == caregiver.id)
    if month:
        from datetime import datetime, timedelta
        try:
            start = datetime.strptime(month, "%Y-%m")
            if start.month == 12:
                end = start.replace(year=start.year+1, month=1)
            else:
                end = start.replace(month=start.month+1)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM.")
        query = query.filter(CaregiverTransaction.created_at >= start, CaregiverTransaction.created_at < end)
    transactions = query.order_by(CaregiverTransaction.created_at.desc()).all()
    return [CaregiverTransactionOut(
        id=str(t.id),
        type=t.type,
        amount=t.amount,
        status=t.status,
        bank_name=t.bank_name,
        account_number=t.account_number,
        account_name=t.account_name,
        reference=t.reference,
        narration=t.narration,
        created_at=t.created_at,
        updated_at=t.updated_at
    ) for t in transactions]


@router.get("/wallet/payslip")
def download_payslip(
    month: str = Query(..., description="Payslip month in YYYY-MM format"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    from datetime import datetime
    try:
        start = datetime.strptime(month, "%Y-%m")
        if start.month == 12:
            end = start.replace(year=start.year+1, month=1)
        else:
            end = start.replace(month=start.month+1)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM.")
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if not caregiver:
        raise HTTPException(status_code=404, detail="Caregiver profile not found.")
    transactions = db.query(CaregiverTransaction).filter(
        CaregiverTransaction.caregiver_id == caregiver.id,
        CaregiverTransaction.type == 'earning',
        CaregiverTransaction.status == 'successful',
        CaregiverTransaction.created_at >= start,
        CaregiverTransaction.created_at < end
    ).order_by(CaregiverTransaction.created_at.asc()).all()
    # Generate CSV
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Amount (NGN)", "Reference", "Narration"])
    for t in transactions:
        writer.writerow([
            t.created_at.strftime("%Y-%m-%d"),
            f"{t.amount/100:,.2f}",
            t.reference or "",
            t.narration or ""
        ])
    output.seek(0)
    filename = f"payslip_{month}.csv"
    return StreamingResponse(output, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/bank-account", response_model=CaregiverBankAccountOut)
def get_bank_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if not caregiver:
        raise HTTPException(status_code=404, detail="Caregiver profile not found.")
    acct = db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver.id).first()
    if not acct:
        raise HTTPException(status_code=404, detail="Bank account not set.")
    return CaregiverBankAccountOut(
        bank_name=acct.bank_name,
        account_number=acct.account_number,
        account_name=acct.account_name,
        created_at=acct.created_at,
        updated_at=acct.updated_at
    )

@router.put("/bank-account", response_model=CaregiverBankAccountOut)
def update_bank_account(
    data: CaregiverBankAccountUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if not caregiver:
        raise HTTPException(status_code=404, detail="Caregiver profile not found.")
    acct = db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver.id).first()
    if not acct:
        acct = CaregiverBankAccount(
            caregiver_id=caregiver.id,
            bank_name=data.bank_name,
            account_number=data.account_number,
            account_name=data.account_name
        )
        db.add(acct)
    else:
        acct.bank_name = data.bank_name
        acct.account_number = data.account_number
        acct.account_name = data.account_name
    db.commit()
    db.refresh(acct)
    return CaregiverBankAccountOut(
        bank_name=acct.bank_name,
        account_number=acct.account_number,
        account_name=acct.account_name,
        created_at=acct.created_at,
        updated_at=acct.updated_at
    )


@router.post("/wallet/withdraw", response_model=CaregiverWithdrawalResponse)
def request_withdrawal(
    data: CaregiverWithdrawalRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if not caregiver:
        raise HTTPException(status_code=404, detail="Caregiver profile not found.")
    if data.amount < settings.MIN_WITHDRAWAL_KOBO:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum withdrawal is {settings.MIN_WITHDRAWAL_KOBO} kobo.",
        )
    # Lock the wallet row so check + deduct is atomic; populate_existing forces a fresh
    # read of the balance after we acquire the lock.
    wallet = (
        db.query(CaregiverWallet)
        .filter(CaregiverWallet.caregiver_id == caregiver.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not wallet or wallet.balance < data.amount:
        db.rollback()
        raise HTTPException(status_code=400, detail="Insufficient wallet balance.")
    acct = db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver.id).first()
    if not acct:
        db.rollback()
        raise HTTPException(status_code=400, detail="Bank account not set.")
    # Create withdrawal transaction (pending)
    from uuid import uuid4
    tx = CaregiverTransaction(
        id=uuid4(),
        caregiver_id=caregiver.id,
        type='withdrawal',
        amount=data.amount,
        status='pending',
        bank_name=acct.bank_name,
        account_number=acct.account_number,
        account_name=acct.account_name,
        narration=data.narration
    )
    wallet.balance -= data.amount
    db.add(tx)
    db.commit()
    db.refresh(tx)
    db.refresh(wallet)
    # In production, trigger payout process here (manual or automated)
    return CaregiverWithdrawalResponse(
        transaction_id=str(tx.id),
        status=tx.status,
        message="Withdrawal request submitted. Processing will be completed soon."
    )
