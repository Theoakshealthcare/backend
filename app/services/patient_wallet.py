import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
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
from app.core.money import kobo_to_naira, to_kobo
from app.services.paystack import PaystackService, paystack_email_for

logger = logging.getLogger(__name__)


class RefundError(Exception):
    """Base class for refund-approval failures. Subclasses map to HTTP statuses."""


class RefundNotFound(RefundError):
    pass


class RefundConflict(RefundError):
    """Already processed, over-refund, or the wallet cannot cover a deposit reversal."""


class RefundGatewayError(RefundError):
    """Paystack definitively refused the refund. Local changes were compensated."""


class RefundOutcomeUnknown(RefundGatewayError):
    """Paystack call timed out / errored ambiguously. The request stays 'processing'
    for manual reconciliation; it is never retried automatically."""


_REFUNDABLE_TYPES = ("payment", "deposit")
_REQUEST_TAG = "[refund_request:"


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

        # ---- phase 1: under row locks, validate and commit the intent -------------------
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
            if original is None or original.type not in _REFUNDABLE_TYPES or original.status != "successful":
                raise RefundConflict("Original transaction is not refundable")

            already_refunded = sum(
                r.amount_kobo for r in self.db.query(PatientRefundRequest).filter(
                    PatientRefundRequest.transaction_id == original.id,
                    PatientRefundRequest.status.in_(("approved", "processing")),
                    PatientRefundRequest.id != refund_request.id,
                ).all()
            )
            if already_refunded + refund_request.amount_kobo > original.amount_kobo:
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
                amount_kobo=refund_request.amount_kobo,
                admin_approved=True,
                admin_id=admin_id,
                approved_at=now,
                refund_reason=refund_request.reason,
            )

            if not original.paystack_reference:
                # Wallet destination: no external call, one transaction.
                wallet.balance_kobo += refund_request.amount_kobo
                wallet.total_refunded_kobo += refund_request.amount_kobo
                wallet.updated_at = now
                refund_transaction.status = "successful"
                refund_transaction.description = f"Refund to wallet - {refund_request.reason}"
                refund_request.status = "approved"
                refund_request.admin_id = admin_id
                refund_request.admin_notes = admin_notes
                refund_request.processed_at = now
                self.db.add(refund_transaction)
                self.db.commit()
                return {"destination": "wallet"}

            # Paystack destination. A deposit's wallet credit is reversed first so the
            # money leaves exactly one place.
            debited = original.type == "deposit"
            if debited:
                if wallet is None or wallet.balance_kobo < refund_request.amount_kobo:
                    raise RefundConflict("Wallet balance cannot cover reversing this deposit")
                wallet.balance_kobo -= refund_request.amount_kobo
                wallet.updated_at = now

            refund_transaction.status = "pending"  # Paystack settles asynchronously
            refund_transaction.description = (
                f"Refund to original payment - {refund_request.reason} {_REQUEST_TAG}{refund_request.id}]"
            )
            refund_request.status = "processing"
            refund_request.admin_id = admin_id
            refund_request.admin_notes = admin_notes
            self.db.add(refund_transaction)
            # capture plain values first: touching ORM attributes after commit would open a new transaction
            refund_tx_id = refund_transaction.id
            request_id = refund_request.id
            reference = original.paystack_reference
            amount_kobo = refund_request.amount_kobo
            self.db.commit()  # releases every lock before the network call
        except Exception:
            self.db.rollback()
            raise

        # ---- phase 2: call Paystack with no transaction open ------------------------------
        result = self.paystack.initiate_refund(reference, amount_kobo)

        # ---- phase 3: finalise or compensate ------------------------------------------------
        if result.get("status"):
            self._finish_refund_request(request_id, refund_tx_id, (result.get("data") or {}).get("id"))
            return {"destination": "paystack"}

        if result.get("ambiguous"):
            logger.error(
                "Paystack refund outcome UNKNOWN for request %s (refund tx %s): %s. "
                "Left in 'processing' for manual reconciliation; not retried.",
                request_id, refund_tx_id, result.get("message"),
            )
            raise RefundOutcomeUnknown("Refund outcome unknown; flagged for reconciliation")

        logger.error("Paystack refund rejected for request %s: %s", request_id, result.get("message"))
        self._compensate_refund(refund_tx_id, to_request_status="pending")
        raise RefundGatewayError("Paystack refund could not be initiated")

    def _finish_refund_request(self, request_id, refund_tx_id, gateway_id) -> None:
        try:
            req = self.db.query(PatientRefundRequest).filter(
                PatientRefundRequest.id == request_id).with_for_update().populate_existing().first()
            tx = self.db.query(PatientTransaction).filter(
                PatientTransaction.id == refund_tx_id).with_for_update().populate_existing().first()
            if gateway_id is not None and tx.paystack_reference is None:
                tx.paystack_reference = str(gateway_id)
            # a refund webhook may already have closed this (failed / successful); don't reopen it
            if req.status == "processing" and tx.status != "failed":
                req.status = "approved"
                req.processed_at = datetime.utcnow()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _compensate_refund(self, refund_tx_id, to_request_status: str) -> bool:
        """Undo a card refund that Paystack did not make: re-credit a reversed deposit and
        mark the refund transaction failed. Idempotent (only acts on a pending refund tx)."""
        try:
            tx = self.db.query(PatientTransaction).filter(
                PatientTransaction.id == refund_tx_id).with_for_update().populate_existing().first()
            if tx is None or tx.status != "pending":
                self.db.rollback()
                return False
            description = tx.description or ""
            req = None
            if _REQUEST_TAG in description:
                req_id = description.rsplit(_REQUEST_TAG, 1)[1].rstrip("]")
                req = self.db.query(PatientRefundRequest).filter(
                    PatientRefundRequest.id == uuid.UUID(req_id)).with_for_update().populate_existing().first()
            original = (
                self.db.query(PatientTransaction).filter(PatientTransaction.id == req.transaction_id)
                .with_for_update().populate_existing().first() if req else None
            )
            if original is not None and original.type == "deposit":
                wallet = self.db.query(PatientWallet).filter(
                    PatientWallet.id == tx.wallet_id).with_for_update().populate_existing().first()
                wallet.balance_kobo += tx.amount_kobo
                wallet.updated_at = datetime.utcnow()
            tx.status = "failed"
            if req is not None:
                req.status = to_request_status
                if to_request_status != "pending":
                    req.processed_at = datetime.utcnow()
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def _match_pending_refund_by_original(self, original_reference: Optional[str], amount_kobo: Optional[int]):
        """Find the single pending refund tx for this original charge reference and amount."""
        if not original_reference or amount_kobo is None:
            return None
        candidates = self.db.query(PatientTransaction).filter(
            PatientTransaction.type == "refund",
            PatientTransaction.status == "pending",
        ).all()
        matches = []
        for t in candidates:
            if t.amount_kobo != amount_kobo or _REQUEST_TAG not in (t.description or ""):
                continue
            try:
                req_id = uuid.UUID((t.description or "").rsplit(_REQUEST_TAG, 1)[1].rstrip("]"))
            except ValueError:
                continue
            req = self.db.query(PatientRefundRequest).filter(PatientRefundRequest.id == req_id).first()
            if req is None:
                continue
            original = self.db.query(PatientTransaction).filter(PatientTransaction.id == req.transaction_id).first()
            if original is not None and original.paystack_reference == original_reference:
                matches.append(t)
        return matches[0] if len(matches) == 1 else None

    def process_refund_webhook(
        self,
        refund_id: Any,
        processed: bool,
        original_reference: Optional[str] = None,
        amount_kobo: Optional[int] = None,
    ) -> bool:
        """Handle Paystack refund.processed / refund.failed. Idempotent.

        Matches by the stored Paystack refund id; if that was never stored (ambiguous
        response, or the webhook beat phase 3), falls back to the original charge
        reference + amount against exactly one pending refund tx and stores the id.
        """
        tx = None
        if refund_id is not None:
            tx = (
                self.db.query(PatientTransaction)
                .filter(PatientTransaction.type == "refund",
                        PatientTransaction.paystack_reference == str(refund_id))
                .first()
            )
        if tx is None:
            tx = self._match_pending_refund_by_original(original_reference, amount_kobo)
            if tx is not None and refund_id is not None:
                tx.paystack_reference = str(refund_id)
                self.db.commit()
        if tx is None:
            self.db.rollback()
            logger.error(
                "Refund webhook could not be matched (refund id %s, original reference %s, amount %s kobo); "
                "needs manual reconciliation", refund_id, original_reference, amount_kobo,
            )
            return False
        tx_id = tx.id
        self.db.rollback()  # re-lock in a fixed order below

        if processed:
            tx = self.db.query(PatientTransaction).filter(
                PatientTransaction.id == tx_id).with_for_update().populate_existing().first()
            if tx.status == "pending":
                tx.status = "successful"
                tx.updated_at = datetime.utcnow()
                description = tx.description or ""
                if _REQUEST_TAG in description:
                    req = self.db.query(PatientRefundRequest).filter(
                        PatientRefundRequest.id == uuid.UUID(description.rsplit(_REQUEST_TAG, 1)[1].rstrip("]"))
                    ).with_for_update().populate_existing().first()
                    if req is not None and req.status == "processing":
                        req.status = "approved"
                        req.processed_at = datetime.utcnow()
            self.db.commit()
            return True
        return self._compensate_refund(tx_id, to_request_status="failed")
