import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import and_, desc

from app.db.models.patient import Patient
from app.db.models.patient import PatientWallet, PatientTransaction, PatientRefundRequest
from app.schemas.patient import (
    PatientDepositRequest, PatientDepositResponse,
    PatientPaymentRequest, PatientPaymentResponse,
    PatientRefundRequestCreate, PatientRefundRequestResponse,
    PatientWalletSummary, PatientTransactionOut,
    PaymentVerificationResponse
)
from app.services.paystack import PaystackService

logger = logging.getLogger(__name__)

_KOBO = Decimal("0.01")


class RefundError(Exception):
    """Base class for refund-approval failures. Subclasses map to HTTP statuses."""


class RefundNotFound(RefundError):
    pass


class RefundConflict(RefundError):
    """Already processed, over-refund, or the wallet cannot cover a deposit reversal."""


class RefundGatewayError(RefundError):
    """Paystack refused or failed to start the refund. Nothing was changed."""


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
            balance=wallet.balance,
            total_deposited=wallet.total_deposited,
            total_spent=wallet.total_spent,
            total_refunded=wallet.total_refunded,
            currency="NGN"
        )

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

        # Get or create wallet
        wallet = self.get_or_create_wallet(patient_id)
        
        # Generate unique reference
        reference = f"DEP_{patient_id}_{uuid.uuid4().hex[:8]}"
        
        # Create pending transaction
        transaction = PatientTransaction(
            id=uuid.uuid4(),
            patient_id=patient_id,
            wallet_id=wallet.id,
            type="deposit",
            amount=deposit_request.amount,
            status="pending",
            paystack_reference=reference,
            description=deposit_request.description or f"Wallet deposit - {deposit_request.amount} NGN"
        )
        
        self.db.add(transaction)
        self.db.commit()
        
        # Initialize Paystack transaction
        paystack_response = self.paystack.initialize_transaction(
            email=patient.email,
            amount=deposit_request.amount,
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
            amount=deposit_request.amount,
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

        paid = paystack_data.get("amount")
        if paid is None or Decimal(str(paid)).quantize(_KOBO) != Decimal(transaction.amount).quantize(_KOBO):
            logger.error(
                "Deposit amount mismatch for %s: expected %s, paystack reported %s",
                reference, transaction.amount, paid,
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
                    wallet.balance += transaction.amount
                    wallet.total_deposited += transaction.amount
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

        self.get_or_create_wallet(patient_id)  # commits; the lock below starts a fresh transaction

        try:
            wallet = (
                self.db.query(PatientWallet)
                .filter(PatientWallet.patient_id == patient_id)
                .with_for_update()
                .populate_existing()
                .first()
            )

            if wallet.balance < payment_request.amount:
                balance = wallet.balance
                self.db.rollback()
                return PatientPaymentResponse(
                    transaction_id="",
                    status="failed",
                    message="Insufficient wallet balance",
                    remaining_balance=balance
                )

            transaction = PatientTransaction(
                id=uuid.uuid4(),
                patient_id=patient_id,
                wallet_id=wallet.id,
                type="payment",
                amount=payment_request.amount,
                status="successful",
                description=payment_request.description or f"Payment - {payment_request.amount} NGN",
                booking_id=payment_request.booking_id
            )

            wallet.balance -= payment_request.amount
            wallet.total_spent += payment_request.amount
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
            remaining_balance=wallet.balance
        )

    def request_refund(
        self, 
        patient_id: uuid.UUID, 
        refund_request: PatientRefundRequestCreate
    ) -> PatientRefundRequestResponse:
        """Request a refund for a transaction"""
        
        # Get the original transaction
        transaction = self.db.query(PatientTransaction).filter(
            and_(
                PatientTransaction.id == refund_request.transaction_id,
                PatientTransaction.patient_id == patient_id,
                PatientTransaction.status == "successful"
            )
        ).first()
        
        if not transaction:
            raise ValueError("Transaction not found or not eligible for refund")
        
        # Check if refund amount is valid
        if refund_request.amount > transaction.amount:
            raise ValueError("Refund amount cannot exceed transaction amount")
        
        # Check if refund request already exists
        existing_request = self.db.query(PatientRefundRequest).filter(
            and_(
                PatientRefundRequest.transaction_id == refund_request.transaction_id,
                PatientRefundRequest.status == "pending"
            )
        ).first()
        
        if existing_request:
            raise ValueError("A pending refund request already exists for this transaction")
        
        # Create refund request
        refund_req = PatientRefundRequest(
            id=uuid.uuid4(),
            patient_id=patient_id,
            transaction_id=refund_request.transaction_id,
            amount=refund_request.amount,
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
                amount=t.amount,
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

    def verify_payment(self, reference: str) -> PaymentVerificationResponse:
        """Verify payment with Paystack"""
        
        paystack_response = self.paystack.verify_transaction(reference)
        
        if not paystack_response.get("status"):
            return PaymentVerificationResponse(
                status="failed",
                message=paystack_response.get("message", "Payment verification failed")
            )
        
        # Get transaction from database
        transaction = self.db.query(PatientTransaction).filter(
            PatientTransaction.paystack_reference == reference
        ).first()
        
        if not transaction:
            return PaymentVerificationResponse(
                status="failed",
                message="Transaction not found in our records"
            )
        
        paystack_data = paystack_response["data"]
        
        # Process the payment based on Paystack status
        if paystack_data["status"] == "success":
            if not self.process_successful_payment(reference, paystack_data):
                return PaymentVerificationResponse(
                    status="failed",
                    message="Payment amount does not match the expected amount",
                    transaction_id=str(transaction.id),
                    amount=transaction.amount
                )
            return PaymentVerificationResponse(
                status="successful",
                message="Payment verified and processed successfully",
                transaction_id=str(transaction.id),
                amount=transaction.amount
            )
        else:
            self.process_failed_payment(reference, paystack_data)
            return PaymentVerificationResponse(
                status="failed",
                message="Payment verification failed",
                transaction_id=str(transaction.id),
                amount=transaction.amount
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
                "amount": req.amount,
                "reason": req.reason,
                "status": req.status,
                "admin_notes": req.admin_notes,
                "created_at": req.created_at,
                "processed_at": req.processed_at
            }
            for req in requests
        ]

    def process_refund_approval(
        self,
        refund_request_id: uuid.UUID,
        admin_id: uuid.UUID,
        action: str,
        admin_notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """Approve or reject a refund request (admin function).

        Approval pays exactly one destination:
          - original transaction has a Paystack reference -> Paystack refund to the original
            payment; if that original was a wallet deposit, the wallet credit is reversed
            (the money leaves the wallet and goes back to the card, never both);
          - otherwise -> credit the wallet.
        The request row is locked for the whole operation, so a second approval waits and
        then sees status != pending. Raises RefundError subclasses; on any error nothing
        has changed.
        """

        refund_request = (
            self.db.query(PatientRefundRequest)
            .filter(PatientRefundRequest.id == refund_request_id)
            .with_for_update()
            .populate_existing()
            .first()
        )

        if not refund_request:
            self.db.rollback()
            raise RefundNotFound("Refund request not found")
        if refund_request.status != "pending":
            self.db.rollback()
            raise RefundConflict("Refund request has already been processed")

        now = datetime.utcnow()

        if action == "reject":
            refund_request.status = "rejected"
            refund_request.admin_id = admin_id
            refund_request.admin_notes = admin_notes
            refund_request.processed_at = now
            self.db.commit()
            return {"destination": None}

        try:
            # Lock the original so concurrent approvals of different requests against the
            # same transaction serialise and the cumulative cap below is race-free.
            original = (
                self.db.query(PatientTransaction)
                .filter(PatientTransaction.id == refund_request.transaction_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if original is None:
                raise RefundConflict("Original transaction not found")

            already_refunded = sum(
                (r.amount for r in self.db.query(PatientRefundRequest).filter(
                    PatientRefundRequest.transaction_id == original.id,
                    PatientRefundRequest.status == "approved",
                    PatientRefundRequest.id != refund_request.id,
                ).all()),
                Decimal("0"),
            )
            if already_refunded + refund_request.amount > original.amount:
                raise RefundConflict("Approved refunds would exceed the original transaction amount")

            wallet = (
                self.db.query(PatientWallet)
                .filter(PatientWallet.id == original.wallet_id)
                .with_for_update()
                .populate_existing()
                .first()
            )

            refund_transaction = PatientTransaction(
                id=uuid.uuid4(),
                patient_id=refund_request.patient_id,
                wallet_id=original.wallet_id,
                type="refund",
                amount=refund_request.amount,
                admin_approved=True,
                admin_id=admin_id,
                approved_at=now,
                refund_reason=refund_request.reason,
            )

            if original.paystack_reference:
                if original.type == "deposit":
                    if wallet is None or wallet.balance < refund_request.amount:
                        raise RefundConflict("Wallet balance cannot cover reversing this deposit")
                    wallet.balance -= refund_request.amount
                    wallet.updated_at = now

                paystack_refund = self.paystack.initiate_refund(
                    original.paystack_reference, refund_request.amount
                )
                if not paystack_refund.get("status"):
                    logger.error(
                        "Paystack refund failed for request %s: %s",
                        refund_request.id, paystack_refund.get("message"),
                    )
                    raise RefundGatewayError("Paystack refund could not be initiated")

                refund_transaction.status = "pending"  # Paystack settles asynchronously
                refund_transaction.description = f"Refund to original payment - {refund_request.reason}"
                gateway_id = (paystack_refund.get("data") or {}).get("id")
                if gateway_id is not None:
                    refund_transaction.paystack_reference = str(gateway_id)
                destination = "paystack"
            else:
                wallet.balance += refund_request.amount
                wallet.total_refunded += refund_request.amount
                wallet.updated_at = now
                refund_transaction.status = "successful"
                refund_transaction.description = f"Refund to wallet - {refund_request.reason}"
                destination = "wallet"

            refund_request.status = "approved"
            refund_request.admin_id = admin_id
            refund_request.admin_notes = admin_notes
            refund_request.processed_at = now
            self.db.add(refund_transaction)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return {"destination": destination}
