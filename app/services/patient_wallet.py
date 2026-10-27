import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, desc
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.patient import Patient
from app.db.models.user import User
from app.db.models.patient import PatientWallet, PatientTransaction, PatientRefundRequest
from app.schemas.patient import (
    PatientDepositRequest, PatientDepositResponse,
    PatientPaymentRequest, PatientPaymentResponse,
    PatientRefundRequestCreate, PatientRefundRequestResponse,
    PatientWalletSummary, PatientTransactionOut,
    PaymentVerificationResponse
)
from app.core.money import kobo_to_naira, to_kobo
from app.services.paystack import PaystackService, paystack_email_for

logger = logging.getLogger(__name__)


_REFUNDABLE_TYPES = ("payment", "deposit")


class PatientWalletService:
    def __init__(self, db: Session):
        self.db = db
        self.paystack = PaystackService()

    def get_or_create_wallet(self, patient_id: uuid.UUID) -> PatientWallet:
        """Get or create patient wallet"""
        wallet = self.db.query(PatientWallet).filter(
            PatientWallet.patient_id == patient_id
        ).first()
        
        if not wallet:
            wallet = PatientWallet(patient_id=patient_id)
            self.db.add(wallet)
            self.db.commit()
            self.db.refresh(wallet)
        
        return wallet

    def get_wallet_summary(self, patient_id: uuid.UUID) -> PatientWalletSummary:
        """Get patient wallet summary"""
        wallet = self.get_or_create_wallet(patient_id)
        
        return PatientWalletSummary(
            balance=kobo_to_naira(wallet.balance_kobo),
            total_deposited=kobo_to_naira(wallet.total_deposited_kobo),
            total_spent=kobo_to_naira(wallet.total_spent_kobo),
            total_refunded=kobo_to_naira(wallet.total_refunded_kobo),
            currency="NGN"
        )

    def _lock_wallet_for_write(self, patient_id: uuid.UUID) -> PatientWallet:
        """Lock the wallet row (the row account erasure locks) before adding a pending deposit or refund, and
        refuse if the account was closed while we waited."""
        self.get_or_create_wallet(patient_id)
        wallet = (
            self.db.query(PatientWallet).filter(PatientWallet.patient_id == patient_id)
            .with_for_update().populate_existing().one()
        )
        active = (
            self.db.query(User.is_active).join(Patient, Patient.user_id == User.id)
            .filter(Patient.id == patient_id).scalar()
        )
        if not active:
            self.db.rollback()
            raise ValueError("This account is closed")
        return wallet

    def initiate_deposit(
        self, 
        patient_id: uuid.UUID, 
        deposit_request: PatientDepositRequest,
        callback_url: Optional[str] = None
    ) -> PatientDepositResponse:
        """Initiate a deposit transaction via Paystack"""
        
        # Get patient details
        patient = self.db.query(Patient).filter(Patient.id == patient_id).first()
        if not patient:
            raise ValueError("Patient not found")

        wallet = self._lock_wallet_for_write(patient_id)
        
        # Naira in, kobo from here on
        amount_kobo = to_kobo(deposit_request.amount)

        # Generate unique reference
        reference = f"DEP_{patient_id}_{uuid.uuid4().hex[:8]}"
        
        # Create pending transaction
        transaction = PatientTransaction(
            id=uuid.uuid4(),
            patient_id=patient_id,
            wallet_id=wallet.id,
            type="deposit",
            amount_kobo=amount_kobo,
            status="pending",
            paystack_reference=reference,
            description=deposit_request.description or f"Wallet deposit - {kobo_to_naira(amount_kobo)} NGN"
        )
        
        self.db.add(transaction)
        self.db.commit()
        
        # Initialize Paystack transaction
        paystack_response = self.paystack.initialize_transaction(
            email=paystack_email_for(patient),
            amount_kobo=amount_kobo,
            reference=reference,
            callback_url=callback_url,
            metadata={
                "patient_id": str(patient_id),
                "transaction_id": str(transaction.id),
                "transaction_type": "deposit"
            }
        )
        
        if not paystack_response.get("status"):
            # Update transaction status to failed
            transaction.status = "failed"
            self.db.commit()
            raise ValueError(f"Payment initialization failed: {paystack_response.get('message')}")
        
        return PatientDepositResponse(
            transaction_id=str(transaction.id),
            paystack_reference=reference,
            authorization_url=paystack_response["authorization_url"],
            access_code=paystack_response["access_code"],
            amount=kobo_to_naira(amount_kobo),
            currency="NGN"
        )

    def process_successful_payment(self, reference: str, paystack_data: Dict[str, Any]) -> bool:
        """Credit a deposit once. Safe to call repeatedly (webhook replay, verify polling).

        Locks the transaction row, so concurrent calls serialise and only the first credits.
        Returns False when the transaction is unknown or the paid amount differs from the
        amount we asked for (nothing is credited in that case).
        """
        transaction = (
            self.db.query(PatientTransaction)
            .filter(PatientTransaction.paystack_reference == reference)
            .with_for_update()
            .populate_existing()
            .first()
        )

        if not transaction:
            self.db.rollback()
            return False

        if transaction.status == "successful":
            self.db.rollback()
            return True  # Already processed

        paid = paystack_data.get("amount_kobo")
        if not isinstance(paid, int) or paid != transaction.amount_kobo:
            logger.error(
                "Deposit amount mismatch for %s: expected %s kobo, paystack reported %s",
                reference, transaction.amount_kobo, paid,
            )
            self.db.rollback()
            return False

        try:
            transaction.status = "successful"
            transaction.paystack_status = paystack_data.get("status")
            transaction.payment_method = paystack_data.get("channel")
            transaction.updated_at = datetime.utcnow()

            if transaction.type == "deposit":
                wallet = (
                    self.db.query(PatientWallet)
                    .filter(PatientWallet.id == transaction.wallet_id)
                    .with_for_update()
                    .populate_existing()
                    .first()
                )
                if wallet:
                    wallet.balance_kobo += transaction.amount_kobo
                    wallet.total_deposited_kobo += transaction.amount_kobo
                    wallet.updated_at = datetime.utcnow()

            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return True

    def process_failed_payment(self, reference: str, paystack_data: Dict[str, Any]) -> bool:
        """Mark a pending deposit failed. A transaction that already settled is left alone."""

        transaction = (
            self.db.query(PatientTransaction)
            .filter(PatientTransaction.paystack_reference == reference)
            .with_for_update()
            .populate_existing()
            .first()
        )

        if not transaction:
            self.db.rollback()
            return False

        if transaction.status != "pending":
            self.db.rollback()
            return True

        transaction.status = "failed"
        transaction.paystack_status = paystack_data.get("status")
        transaction.description = f"{transaction.description} - Failed: {paystack_data.get('gateway_response', 'Unknown error')}"
        transaction.updated_at = datetime.utcnow()

        self.db.commit()
        return True

    def make_payment(
        self,
        patient_id: uuid.UUID,
        payment_request: PatientPaymentRequest
    ) -> PatientPaymentResponse:
        """Make a payment from wallet balance (one transaction, wallet row locked)."""

        amount_kobo = to_kobo(payment_request.amount)
        self.get_or_create_wallet(patient_id)  # commits; the lock below starts a fresh transaction

        try:
            wallet = (
                self.db.query(PatientWallet)
                .filter(PatientWallet.patient_id == patient_id)
                .with_for_update()
                .populate_existing()
                .first()
            )

            if wallet.balance_kobo < amount_kobo:
                balance_kobo = wallet.balance_kobo
                self.db.rollback()
                return PatientPaymentResponse(
                    transaction_id="",
                    status="failed",
                    message="Insufficient wallet balance",
                    remaining_balance=kobo_to_naira(balance_kobo)
                )

            transaction = PatientTransaction(
                id=uuid.uuid4(),
                patient_id=patient_id,
                wallet_id=wallet.id,
                type="payment",
                amount_kobo=amount_kobo,
                status="successful",
                description=payment_request.description or f"Payment - {kobo_to_naira(amount_kobo)} NGN",
                booking_id=payment_request.booking_id
            )

            wallet.balance_kobo -= amount_kobo
            wallet.total_spent_kobo += amount_kobo
            wallet.updated_at = datetime.utcnow()

            self.db.add(transaction)
            self.db.commit()
            self.db.refresh(wallet)

        except Exception:
            self.db.rollback()
            raise

        return PatientPaymentResponse(
            transaction_id=str(transaction.id),
            status="successful",
            message="Payment completed successfully",
            remaining_balance=kobo_to_naira(wallet.balance_kobo)
        )

    def request_refund(
        self, 
        patient_id: uuid.UUID, 
        refund_request: PatientRefundRequestCreate
    ) -> PatientRefundRequestResponse:
        """Request a refund for a transaction"""
        self._lock_wallet_for_write(patient_id)
        
        # Get the original transaction
        transaction = self.db.query(PatientTransaction).filter(
            and_(
                PatientTransaction.id == refund_request.transaction_id,
                PatientTransaction.patient_id == patient_id,
                PatientTransaction.status == "successful",
                PatientTransaction.type.in_(_REFUNDABLE_TYPES)
            )
        ).first()
        
        if not transaction:
            raise ValueError("Transaction not found or not eligible for refund")
        
        # Check if refund amount is valid
        amount_kobo = to_kobo(refund_request.amount)
        if amount_kobo > transaction.amount_kobo:
            raise ValueError("Refund amount cannot exceed transaction amount")
        
        # Check if refund request already exists
        existing_request = self.db.query(PatientRefundRequest).filter(
            and_(
                PatientRefundRequest.transaction_id == refund_request.transaction_id,
                PatientRefundRequest.status.in_(("pending", "processing"))
            )
        ).first()
        
        if existing_request:
            raise ValueError("A pending refund request already exists for this transaction")
        
        # Create refund request
        refund_req = PatientRefundRequest(
            id=uuid.uuid4(),
            patient_id=patient_id,
            transaction_id=refund_request.transaction_id,
            amount_kobo=amount_kobo,
            reason=refund_request.reason,
            status="pending"
        )
        
        self.db.add(refund_req)
        self.db.commit()
        
        return PatientRefundRequestResponse(
            request_id=str(refund_req.id),
            status="pending",
            message="Refund request submitted successfully. It will be reviewed by our team."
        )

    def get_transactions(
        self, 
        patient_id: uuid.UUID, 
        month: Optional[str] = None,
        transaction_type: Optional[str] = None
    ) -> List[PatientTransactionOut]:
        """Get patient transaction history"""
        
        query = self.db.query(PatientTransaction).filter(
            PatientTransaction.patient_id == patient_id
        )
        
        # Filter by month if provided
        if month:
            try:
                start = datetime.strptime(month, "%Y-%m")
                if start.month == 12:
                    end = start.replace(year=start.year + 1, month=1)
                else:
                    end = start.replace(month=start.month + 1)
                
                query = query.filter(
                    and_(
                        PatientTransaction.created_at >= start,
                        PatientTransaction.created_at < end
                    )
                )
            except ValueError:
                raise ValueError("Invalid month format. Use YYYY-MM.")
        
        # Filter by transaction type if provided
        if transaction_type:
            query = query.filter(PatientTransaction.type == transaction_type)
        
        transactions = query.order_by(desc(PatientTransaction.created_at)).all()
        
        return [
            PatientTransactionOut(
                id=str(t.id),
                type=t.type,
                amount=kobo_to_naira(t.amount_kobo),
                status=t.status,
                paystack_reference=t.paystack_reference,
                payment_method=t.payment_method,
                description=t.description,
                booking_id=str(t.booking_id) if t.booking_id else None,
                created_at=t.created_at,
                updated_at=t.updated_at
            )
            for t in transactions
        ]

    def verify_payment(self, reference: str, patient_id: uuid.UUID) -> PaymentVerificationResponse:
        """Verify one of the caller's own payments with Paystack. Raises LookupError for an
        unknown or someone else's reference (before anything is sent to Paystack)."""

        transaction = self.db.query(PatientTransaction).filter(
            PatientTransaction.paystack_reference == reference,
            PatientTransaction.patient_id == patient_id,
        ).first()

        if not transaction:
            raise LookupError("Transaction not found")

        paystack_response = self.paystack.verify_transaction(reference)

        if not paystack_response.get("status"):
            return PaymentVerificationResponse(
                status="failed",
                message=paystack_response.get("message", "Payment verification failed")
            )

        paystack_data = paystack_response["data"]
        
        # Process the payment based on Paystack status
        if paystack_data["status"] == "success":
            if not self.process_successful_payment(reference, paystack_data):
                return PaymentVerificationResponse(
                    status="failed",
                    message="Payment amount does not match the expected amount",
                    transaction_id=str(transaction.id),
                    amount=kobo_to_naira(transaction.amount_kobo)
                )
            return PaymentVerificationResponse(
                status="successful",
                message="Payment verified and processed successfully",
                transaction_id=str(transaction.id),
                amount=kobo_to_naira(transaction.amount_kobo)
            )
        else:
            self.process_failed_payment(reference, paystack_data)
            return PaymentVerificationResponse(
                status="failed",
                message="Payment verification failed",
                transaction_id=str(transaction.id),
                amount=kobo_to_naira(transaction.amount_kobo)
            )

    def get_refund_requests(self, patient_id: uuid.UUID) -> List[Dict[str, Any]]:
        """Get patient's refund requests"""
        
        requests = self.db.query(PatientRefundRequest).filter(
            PatientRefundRequest.patient_id == patient_id
        ).order_by(desc(PatientRefundRequest.created_at)).all()
        
        return [
            {
                "id": str(req.id),
                "transaction_id": str(req.transaction_id),
                "amount": kobo_to_naira(req.amount_kobo),
                "reason": req.reason,
                "status": req.status,
                "admin_notes": req.admin_notes,
                "created_at": req.created_at,
                "processed_at": req.processed_at
            }
            for req in requests
        ]


def credit_refund_locked(db: Session, patient_id: uuid.UUID, amount_kobo: int, refund_id: uuid.UUID) -> PatientTransaction:
    """Credit a refund to the patient's Oakly wallet inside the caller's transaction. Never commits.

    Locks the wallet row itself (``FOR UPDATE`` + ``populate_existing``, so a cached copy is never used), creating it first
    when the patient has none. The create is ``INSERT .. ON CONFLICT DO NOTHING`` so two concurrent first credits cannot
    both create it, and nothing is committed mid-transaction. Lock order: this is the LAST lock, after booking and refund.
    """
    if amount_kobo <= 0:
        raise ValueError("a wallet credit must be positive")
    db.execute(pg_insert(PatientWallet).values(id=uuid.uuid4(), patient_id=patient_id, balance_kobo=0,
                                               total_deposited_kobo=0, total_spent_kobo=0, total_refunded_kobo=0,
                                               is_active=True, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
               .on_conflict_do_nothing(index_elements=[PatientWallet.patient_id]))
    wallet = (db.query(PatientWallet).filter(PatientWallet.patient_id == patient_id)
              .with_for_update().populate_existing().one())
    wallet.balance_kobo = int(wallet.balance_kobo) + amount_kobo
    wallet.total_refunded_kobo = int(wallet.total_refunded_kobo) + amount_kobo
    txn = PatientTransaction(patient_id=patient_id, wallet_id=wallet.id, type="refund", status="successful",
                             amount_kobo=amount_kobo, refund_id=refund_id, description="Refund to your Oakly wallet")
    db.add(txn)
    db.flush()
    return txn
