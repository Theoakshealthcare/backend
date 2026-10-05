import requests
import hashlib
import hmac
import json
from typing import Dict, Any, Optional
from datetime import datetime

from app.core.config import settings

REQUEST_TIMEOUT_SECONDS = 15  # Assume you have Paystack keys in settings


class PaystackService:
    def __init__(self):
        self.secret_key = settings.PAYSTACK_SECRET_KEY
        self.public_key = settings.PAYSTACK_PUBLIC_KEY
        self.base_url = "https://api.paystack.co"
        self.headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }

    def initialize_transaction(
        self, 
        email: str, 
        amount_kobo: int, 
        reference: str,
        callback_url: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Initialize a Paystack transaction
        
        Args:
            email: Customer email
            amount_kobo: Amount to charge, in integer kobo (sent to Paystack as-is)
            reference: Unique transaction reference
            callback_url: URL to redirect to after payment
            metadata: Additional data to store with transaction
        
        Returns:
            Dict containing authorization_url, access_code, and reference
        """
        url = f"{self.base_url}/transaction/initialize"
        
        payload = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "currency": "NGN"
        }
        
        if callback_url:
            payload["callback_url"] = callback_url
            
        if metadata:
            payload["metadata"] = metadata
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            
            data = response.json()
            if data.get("status"):
                return {
                    "status": True,
                    "authorization_url": data["data"]["authorization_url"],
                    "access_code": data["data"]["access_code"],
                    "reference": data["data"]["reference"]
                }
            else:
                return {
                    "status": False,
                    "message": data.get("message", "Transaction initialization failed")
                }
                
        except requests.RequestException as e:
            return {
                "status": False,
                "message": f"Network error: {str(e)}"
            }
        except Exception as e:
            return {
                "status": False,
                "message": f"Unexpected error: {str(e)}"
            }

    def verify_transaction(self, reference: str) -> Dict[str, Any]:
        """
        Verify a Paystack transaction
        
        Args:
            reference: Transaction reference to verify
            
        Returns:
            Dict containing transaction details
        """
        url = f"{self.base_url}/transaction/verify/{reference}"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            
            data = response.json()
            if data.get("status"):
                transaction_data = data["data"]
                return {
                    "status": True,
                    "data": {
                        "id": transaction_data["id"],
                        "reference": transaction_data["reference"],
                        "amount_kobo": int(transaction_data["amount"]),  # integer kobo, as Paystack reports it
                        "status": transaction_data["status"],
                        "paid_at": transaction_data.get("paid_at"),
                        "channel": transaction_data.get("channel"),
                        "currency": transaction_data.get("currency"),
                        "customer": transaction_data.get("customer"),
                        "authorization": transaction_data.get("authorization"),
                        "metadata": transaction_data.get("metadata", {})
                    }
                }
            else:
                return {
                    "status": False,
                    "message": data.get("message", "Transaction verification failed")
                }
                
        except requests.RequestException as e:
            return {
                "status": False,
                "message": f"Network error: {str(e)}"
            }
        except Exception as e:
            return {
                "status": False,
                "message": f"Unexpected error: {str(e)}"
            }

    def initiate_refund(self, transaction_reference: str, amount_kobo: Optional[int] = None) -> Dict[str, Any]:
        """
        Initiate a refund for a transaction
        
        Args:
            transaction_reference: Reference of transaction to refund
            amount_kobo: Amount to refund in integer kobo (if partial). If None, full refund
            
        Returns:
            Dict containing refund details
        """
        url = f"{self.base_url}/refund"
        
        payload = {
            "transaction": transaction_reference
        }
        
        if amount_kobo:
            payload["amount"] = int(amount_kobo)
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            
            data = response.json()
            if data.get("status"):
                return {
                    "status": True,
                    "data": data["data"]
                }
            else:
                return {
                    "status": False,
                    "message": data.get("message", "Refund initiation failed")
                }
                
        except requests.HTTPError as e:
            # A 4xx is Paystack definitively refusing; a 5xx may or may not have been applied.
            code = e.response.status_code if e.response is not None else None
            definite = code is not None and 400 <= code < 500
            return {
                "status": False,
                "ambiguous": not definite,
                "message": f"Paystack error: {code}"
            }
        except requests.RequestException as e:
            # Timeout / connection drop: the refund may have been created. Never assume failure.
            return {
                "status": False,
                "ambiguous": True,
                "message": f"Network error: {str(e)}"
            }
        except Exception as e:
            return {
                "status": False,
                "ambiguous": True,
                "message": f"Unexpected error: {str(e)}"
            }

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verify Paystack webhook signature
        
        Args:
            payload: Raw request body
            signature: X-Paystack-Signature header value
            
        Returns:
            True if signature is valid, False otherwise
        """
        if not self.secret_key or not signature:
            # With no key configured the HMAC is computable by anyone: never accept.
            return False
        try:
            expected_signature = hmac.new(
                self.secret_key.encode('utf-8'),
                payload,
                hashlib.sha512
            ).hexdigest()
            
            return hmac.compare_digest(expected_signature, signature)
        except Exception:
            return False

    def parse_webhook_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse webhook event data
        
        Args:
            payload: Webhook payload
            
        Returns:
            Parsed event data
        """
        event = payload.get("event")
        data = payload.get("data", {})
        
        if event == "charge.success":
            return {
                "event": "payment_successful",
                "reference": data.get("reference"),
                "amount_kobo": data.get("amount"),
                "status": data.get("status"),
                "paid_at": data.get("paid_at"),
                "channel": data.get("channel"),
                "customer_email": data.get("customer", {}).get("email"),
                "metadata": data.get("metadata", {})
            }
        elif event == "charge.failed":
            return {
                "event": "payment_failed",
                "reference": data.get("reference"),
                "amount_kobo": data.get("amount"),
                "status": data.get("status"),
                "gateway_response": data.get("gateway_response"),
                "customer_email": data.get("customer", {}).get("email"),
                "metadata": data.get("metadata", {})
            }
        elif event in ("refund.processed", "refund.failed"):
            return {
                "event": "refund_processed" if event == "refund.processed" else "refund_failed",
                "refund_id": data.get("id"),
                "reference": data.get("transaction_reference") or (data.get("transaction") or {}).get("reference"),
                "amount_kobo": data.get("amount"),
                "status": data.get("status"),
            }
        elif event == "refund.successful":
            return {
                "event": "refund_successful",
                "reference": data.get("transaction", {}).get("reference"),
                "amount_kobo": data.get("amount"),
                "status": data.get("status"),
                "refunded_at": data.get("created_at"),
                "customer_email": data.get("customer", {}).get("email")
            }
        else:
            return {
                "event": event,
                "data": data
            }

    def get_supported_banks(self) -> Dict[str, Any]:
        """
        Get list of supported banks for bank transfers
        
        Returns:
            Dict containing list of supported banks
        """
        url = f"{self.base_url}/bank"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            
            data = response.json()
            if data.get("status"):
                return {
                    "status": True,
                    "banks": data["data"]
                }
            else:
                return {
                    "status": False,
                    "message": "Failed to fetch supported banks"
                }
                
        except requests.RequestException as e:
            return {
                "status": False,
                "message": f"Network error: {str(e)}"
            }
        except Exception as e:
            return {
                "status": False,
                "message": f"Unexpected error: {str(e)}"
            }