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
        """Process successful payment from webhook"""
        
        transaction = self.db.query(PatientTransaction).filter(
            PatientTransaction.paystack_reference == reference
        ).first()
        
        if not transaction:
            return False
        
        if transaction.status == "successful":
            return True  # Already processed
        
        try:
            # Begin transaction
            self.db.begin()
            
            # Update transaction
            transaction.status = "successful"
            transaction.paystack_status = paystack_data.get("status")
            transaction.payment_method = paystack_data.get("channel")
            transaction.updated_at = datetime.utcnow()
            
            # Update wallet balance if it's a deposit
            if transaction.type == "deposit":
                wallet = self.db.query(PatientWallet).filter(
                    PatientWallet.id == transaction.wallet_id
                ).with_for_update().first()  # Lock the row for update
                
                if wallet:
                    wallet.balance += transaction.amount
                    wallet.total_deposited += transaction.amount
                    wallet.updated_at = datetime.utcnow()
            
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            raise e
        return True

    def process_failed_payment(self, reference: str, paystack_data: Dict[str, Any]) -> bool:
        """Process failed payment from webhook"""
        
        transaction = self.db.query(PatientTransaction).filter(
            PatientTransaction.paystack_reference == reference
        ).first()
        
        if not transaction:
            return False
        
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
        """Make a payment from wallet balance"""
        
        wallet = self.get_or_create_wallet(patient_id)
        
        # Check if sufficient balance
        if wallet.balance < payment_request.amount:
            return PatientPaymentResponse(
                transaction_id="",
                status="failed",
                message="Insufficient wallet balance",
                remaining_balance=wallet.balance
            )
        
        try:
            # Begin atomic transaction
            self.db.begin()
            
            # Lock wallet for update to prevent race conditions
            wallet = self.db.query(PatientWallet).filter(
                PatientWallet.patient_id == patient_id
            ).with_for_update().first()
            
            # Double-check balance after locking
            if wallet.balance < payment_request.amount:
                self.db.rollback()
                return PatientPaymentResponse(
                    transaction_id="",
                    status="failed",
                    message="Insufficient wallet balance",
                    remaining_balance=wallet.balance
                )
            
            # Create payment transaction
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
            
            # Update wallet balance
            wallet.balance -= payment_request.amount
            wallet.total_spent += payment_request.amount
            wallet.updated_at = datetime.utcnow()
            
            self.db.add(transaction)
            self.db.commit()
            self.db.refresh(wallet)
            
        except Exception as e:
            self.db.rollback()
            raise e
        
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
            self.process_successful_payment(reference, paystack_data)
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
    ) -> bool:
        """Process refund request approval/rejection (Admin function)"""
        
        refund_request = self.db.query(PatientRefundRequest).filter(
            PatientRefundRequest.id == refund_request_id
        ).first()
        
        if not refund_request or refund_request.status != "pending":
            return False
        
        if action == "approve":
            # Create refund transaction
            refund_transaction = PatientTransaction(
                id=uuid.uuid4(),
                patient_id=refund_request.patient_id,
                wallet_id=self.get_or_create_wallet(refund_request.patient_id).id,
                type="refund",
                amount=refund_request.amount,
                status="successful",
                description=f"Refund for transaction - {refund_request.reason}",
                admin_approved=True,
                admin_id=admin_id,
                approved_at=datetime.utcnow()
            )
            
            # Update wallet balance
            wallet = self.get_or_create_wallet(refund_request.patient_id)
            wallet.balance += refund_request.amount
            wallet.total_refunded += refund_request.amount
            wallet.updated_at = datetime.utcnow()
            
            # Update refund request
            refund_request.status = "approved"
            refund_request.admin_id = admin_id
            refund_request.admin_notes = admin_notes
            refund_request.processed_at = datetime.utcnow()
            
            self.db.add(refund_transaction)
            
            # If original transaction was via Paystack, initiate Paystack refund
            original_transaction = self.db.query(PatientTransaction).filter(
                PatientTransaction.id == refund_request.transaction_id
            ).first()
            
            if original_transaction and original_transaction.paystack_reference:
                paystack_refund = self.paystack.initiate_refund(
                    original_transaction.paystack_reference,
                    refund_request.amount
                )
                
                if paystack_refund.get("status"):
                    refund_transaction.paystack_reference = paystack_refund["data"].get("id")
        
        elif action == "reject":
            refund_request.status = "rejected"
            refund_request.admin_id = admin_id
            refund_request.admin_notes = admin_notes
            refund_request.processed_at = datetime.utcnow()
        
        self.db.commit()
        return True