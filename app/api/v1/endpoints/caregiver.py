from datetime import datetime, timedelta
from typing import Any, Dict, List, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, StreamingResponse
import csv
from io import StringIO
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import get_current_caregiver, get_current_user, get_db, get_user_or_admin, is_admin
from app.db.models.admin import AdminUser
from app.db.models.user import User
from app.schemas.caregiver import (
    FormOptionsOut,
    VerificationStatusOut,
    CaregiverProfile,
    CaregiverProfileStep1,
    CaregiverProfileStep2,
    CaregiverConsentData,
    DocumentUploadResponse,
    IdentityVerificationResponse,
    SpecializationOptions,
    IdentityVerificationClientEventRequest,
    IdentityVerificationSessionOut,
    IdentityVerificationSessionRequest,
    IdentityVerificationStateOut,
    CaregiverWalletSummary,
    CaregiverTransactionOut,
    BankAccountResolveOut,
    BankAccountResolveRequest,
    BankOut,
    CaregiverBankAccountOut,
    CaregiverBankAccountUpdate,
)
from app.services.caregiver import CaregiverService
from app.services.kyc_availability import ProviderAvailability, get_provider_availability
from app.services.kyc_caregiver import CaregiverIdentityService, KycError
from app.services import bank_accounts, booking_policy, payouts
from app.services import earnings as earnings_service
from app.db.models.caregiver import EARNING_TYPES, CaregiverWallet, CaregiverTransaction, CaregiverBankAccount, Caregiver, CaregiverDocument

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


@router.get("/verification-status", response_model=VerificationStatusOut)
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


@router.get("/form-options", response_model=FormOptionsOut)
def get_form_options() -> Any:
    """Get form options for dropdowns (specializations, service areas, etc.)"""
    return CaregiverService.get_form_options()


def _identity_service(db: Session = Depends(get_db),
                      availability: ProviderAvailability = Depends(get_provider_availability)) -> CaregiverIdentityService:
    return CaregiverIdentityService(db, availability)


def _kyc_http_error(exc: KycError) -> HTTPException:
    """Identity verification errors carry a machine code: ``detail`` is ``{"code", "message"}`` (the rest of this API uses a string)."""
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


_KYC_ERRORS = {
    404: {"description": "No caregiver profile, or (client-event) no such attempt of yours: VERIFICATION_NOT_FOUND."},
    409: {"description": "ALREADY_VERIFIED, UNDER_REVIEW, IDENTITY_REJECTED, PROFILE_INCOMPLETE, POLICY_VERSION_MISMATCH, "
                        "ATTEMPTS_EXHAUSTED or PROVIDER_AVAILABLE."},
}


@router.get("/identity-verification", response_model=IdentityVerificationStateOut, responses={404: _KYC_ERRORS[404]})
def get_identity_verification(
    current_user: User = Depends(get_current_caregiver),
    service: CaregiverIdentityService = Depends(_identity_service),
) -> Any:
    """Where the caregiver stands in identity verification. Never returns scores or the reason for a review."""
    try:
        return service.status_for(current_user.id)
    except KycError as exc:
        raise _kyc_http_error(exc)


@router.post("/identity-verification/sessions", response_model=IdentityVerificationSessionOut, status_code=status.HTTP_201_CREATED,
             responses={200: {"description": "The caregiver's active attempt, returned again."}, **_KYC_ERRORS,
                        503: {"description": "PROVIDER_UNAVAILABLE: Prembly is flagged down or the widget is not configured."}})
def create_identity_verification_session(
    payload: IdentityVerificationSessionRequest,
    response: Response,
    current_user: User = Depends(get_current_caregiver),
    service: CaregiverIdentityService = Depends(_identity_service),
) -> Any:
    """Record the separate identity consent and open an attempt (or resume the active one); returns the widget config."""
    try:
        body, created = service.start_session(current_user.id, payload.policy_version)
    except KycError as exc:
        raise _kyc_http_error(exc)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return body


@router.post("/identity-verification/defer", response_model=IdentityVerificationStateOut, responses=_KYC_ERRORS)
def defer_identity_verification(
    current_user: User = Depends(get_current_caregiver),
    service: CaregiverIdentityService = Depends(_identity_service),
) -> Any:
    """Outage path: only while Prembly is flagged unavailable. Lets the caregiver carry on to consent; verifies nobody."""
    try:
        return service.defer(current_user.id)
    except KycError as exc:
        raise _kyc_http_error(exc)


@router.post("/identity-verification/{verification_id}/client-event", response_model=IdentityVerificationStateOut,
             responses=_KYC_ERRORS)
def identity_verification_client_event(
    verification_id: UUID,
    payload: IdentityVerificationClientEventRequest,
    current_user: User = Depends(get_current_caregiver),
    service: CaregiverIdentityService = Depends(_identity_service),
) -> Any:
    """A hint from the widget (opened, completed, closed, error). Non-authoritative: it can advance the caregiver's own
    attempt and remember the session id, and can never change the caregiver's identity status."""
    try:
        return service.client_event(current_user.id, verification_id, payload.event, payload.session_id)
    except KycError as exc:
        raise _kyc_http_error(exc)


@router.post("/identity-verification/result", status_code=status.HTTP_410_GONE, include_in_schema=True,
             responses={410: {"description": "The client no longer reports verification results."}})
def receive_verification_result(current_user: User = Depends(get_current_user)):
    """Removed. The verification result comes from the provider's signed webhook, not from the app. Kept as a clear 410 so an
    older app version shows a message instead of an unexplained 404. Takes no body: nothing the app posts is read or stored."""
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="The app no longer reports identity verification results. Verification is completed in the verification step "
               "and confirmed by our verification partner.")


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
        # The release job creates wallets the same way: a concurrent insert must not turn into a 500.
        db.execute(pg_insert(CaregiverWallet).values(caregiver_id=caregiver.id, balance=0).on_conflict_do_nothing(
            index_elements=["caregiver_id"]))
        db.commit()
        wallet = db.query(CaregiverWallet).filter(CaregiverWallet.caregiver_id == caregiver.id).one()
    total_earned = db.query(CaregiverTransaction).filter(
        CaregiverTransaction.caregiver_id == caregiver.id,
        CaregiverTransaction.type.in_(EARNING_TYPES),
        CaregiverTransaction.status == 'successful'
    ).with_entities(func.coalesce(func.sum(CaregiverTransaction.amount), 0)).scalar()
    total_withdrawn = db.query(CaregiverTransaction).filter(
        CaregiverTransaction.caregiver_id == caregiver.id,
        CaregiverTransaction.type.in_(('withdrawal', 'payout')),
        CaregiverTransaction.status == 'successful'
    ).with_entities(func.coalesce(func.sum(CaregiverTransaction.amount), 0)).scalar()
    bal = earnings_service.balances(db, caregiver.id, wallet)
    policy = booking_policy.current(db)
    acct = db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver.id).first()
    return CaregiverWalletSummary(
        balance=bal.available_kobo,
        total_earned=total_earned or 0,
        total_withdrawn=total_withdrawn or 0,
        currency="NGN",
        available_balance=bal.available_kobo,
        pending_balance=bal.pending_kobo,
        held_balance=bal.held_kobo,
        total_pending=bal.pending_kobo + bal.held_kobo,
        next_available_at=bal.next_available_at,
        payout_cadence=policy.payout_cadence,
        payout_min_kobo=policy.payout_min_naira * 100,
        next_payout_on=payouts.next_payout_date(policy.payout_cadence, datetime.utcnow().date()),
        payout_in_progress_kobo=payouts.in_progress_kobo(db, caregiver.id),
        payouts_blocked_until=acct.payout_blocked_until if bank_accounts.is_cooling_down(acct) else None,
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
        booking_id=str(t.booking_id) if t.booking_id else None,
        available_at=t.available_at,
        released_at=t.released_at,
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
        CaregiverTransaction.type.in_(EARNING_TYPES),
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


def _caregiver_or_403(current_user: User, db: Session) -> Caregiver:
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if not caregiver:
        raise HTTPException(status_code=404, detail="Caregiver profile not found.")
    return caregiver


def _bank_error(exc: bank_accounts.BankError) -> HTTPException:
    if isinstance(exc, bank_accounts.BankThrottled):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    if isinstance(exc, bank_accounts.BankUnavailable):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _bank_out(acct: CaregiverBankAccount) -> CaregiverBankAccountOut:
    return CaregiverBankAccountOut(
        bank_name=acct.bank_name,
        bank_code=acct.bank_code,
        account_number=acct.account_number,
        account_name=acct.account_name,
        verified=bool(acct.recipient_code),
        payouts_blocked_until=acct.payout_blocked_until if bank_accounts.is_cooling_down(acct) else None,
        created_at=acct.created_at,
        updated_at=acct.updated_at
    )


@router.get("/banks", response_model=List[BankOut])
def list_banks(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Banks a payout can go to (from Paystack, cached for a day). The app shows the names and sends back the code."""
    _caregiver_or_403(current_user, db)
    db.rollback()  # no transaction is held while Paystack is called
    try:
        return bank_accounts.list_banks()
    except bank_accounts.BankError as exc:
        raise _bank_error(exc)


@router.post("/bank-account/resolve", response_model=BankAccountResolveOut)
def resolve_bank_account(
    data: BankAccountResolveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check an account number and bank and return the name the bank holds for it, so the caregiver can confirm it is
    theirs before saving. Rate limited per caregiver."""
    caregiver = _caregiver_or_403(current_user, db)
    caregiver_id = caregiver.id
    db.rollback()
    try:
        resolved = bank_accounts.resolve(caregiver_id, data.account_number, data.bank_code)
    except bank_accounts.BankError as exc:
        raise _bank_error(exc)
    return BankAccountResolveOut(account_name=resolved.account_name, account_number=resolved.account_number,
                                 bank_code=resolved.bank_code, bank_name=resolved.bank_name)


@router.get("/bank-account", response_model=CaregiverBankAccountOut)
def get_bank_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caregiver = _caregiver_or_403(current_user, db)
    acct = db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver.id).first()
    if not acct:
        raise HTTPException(status_code=404, detail="Bank account not set.")
    return _bank_out(acct)


@router.put("/bank-account", response_model=CaregiverBankAccountOut)
def update_bank_account(
    data: CaregiverBankAccountUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the payout account. The server resolves the account holder's name itself (a name sent by the client is never
    used) and registers the account with Paystack. Changing to a different account, or saving the first one while there is money in the wallet,
    blocks payouts for 24 hours."""
    caregiver = _caregiver_or_403(current_user, db)
    db.rollback()
    try:
        result = bank_accounts.save(db, caregiver, data.account_number, data.bank_code, request=request)
    except bank_accounts.BankError as exc:
        raise _bank_error(exc)
    return _bank_out(result.account)


@router.post("/wallet/withdraw", status_code=status.HTTP_410_GONE, include_in_schema=True,
             responses={410: {"description": "Self-withdrawal was replaced by scheduled payouts."}})
def request_withdrawal(current_user: User = Depends(get_current_user)):
    """Removed. Caregivers no longer withdraw: the available balance is paid to their bank account on the payout schedule
    (A3 slice 2). Kept as a clear 410 so an older app version shows a message instead of an unexplained 404."""
    if current_user.user_type != "caregiver":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied. Caregiver account required.")
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Withdrawals are no longer available. Your available balance is paid to your bank account automatically "
               "on the payout schedule.")
