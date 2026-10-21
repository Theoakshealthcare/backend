import requests
import hashlib
import hmac
import json
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from app.core.config import settings
from app.core.phone import contact_ref

REQUEST_TIMEOUT_SECONDS = 15  # Assume you have Paystack keys in settings


PAYSTACK_PLACEHOLDER_DOMAIN = "pay.oaklycare.com"


def paystack_email_for(user: Any) -> str:
    """The address to hand Paystack for a payer (``user`` is a User or a Patient). Paystack insists on an
    email; a phone-only (WhatsApp) payer has none, so they get a deterministic non-deliverable one derived
    from the keyed contact ref: stable per person (their payments group together), and the phone number
    never goes to a third party in the clear."""
    if getattr(user, "email", None):
        return user.email
    digits = "".join(ch for ch in (getattr(user, "phone_number", None) or "") if ch.isdigit())
    if not digits:
        raise ValueError("a payer needs an email or a phone number")
    if digits.startswith("0"):
        digits = "234" + digits[1:]  # a national-format Nigerian number
    return f"wa-{contact_ref(digits)}@{PAYSTACK_PLACEHOLDER_DOMAIN}"


class PaystackService:
    def __init__(self):
        self.secret_key = settings.PAYSTACK_SECRET_KEY
        self.public_key = settings.PAYSTACK_PUBLIC_KEY
        self.base_url = settings.PAYSTACK_BASE_URL.rstrip("/")
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
                        "fees_kobo": transaction_data.get("fees"),  # Paystack's fee, integer kobo (null when not reported)
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
            # "unreachable" = we could not get an answer (timeout, connection, 5xx, rate limit), as opposed to
            # Paystack answering definitively that the transaction is not a success (e.g. 404 for an unknown reference).
            reply = getattr(e, "response", None)
            code = getattr(reply, "status_code", None)
            definitive = code is not None and 400 <= code < 500 and code != 429
            result = {
                "status": False,
                "message": f"Network error: {str(e)}"
            }
            if not definitive:
                result["unreachable"] = True
            return result
        except Exception as e:
            return {
                "status": False,
                "unreachable": True,
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
                "fees_kobo": data.get("fees"),  # Paystack's fee, integer kobo (null when not reported)
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
        elif event in ("refund.pending", "refund.processing", "refund.needs-attention", "refund.processed", "refund.failed"):
            # Refunds v2: every refund status event goes to ``refunds.apply_refund_event``. The refund id is ``data.id``,
            # ``refund_reference`` is null, and the amount can arrive as a string.
            return {
                "event": "refund_event",
                "refund_status": event.split(".", 1)[1],
                "refund_id": data.get("id"),
                "reference": data.get("transaction_reference") or (data.get("transaction") or {}).get("reference"),
                "amount_kobo": data.get("amount"),
                "status": data.get("status"),
                "reason": data.get("merchant_note") or data.get("customer_note"),
            }
        elif event in ("transfer.success", "transfer.failed", "transfer.reversed"):
            return {
                "event": event.replace(".", "_"),  # transfer_success | transfer_failed | transfer_reversed
                "reference": data.get("reference"),
                "transfer_code": data.get("transfer_code"),
                "amount_kobo": data.get("amount"),
                "status": data.get("status"),
                "reason": data.get("reason"),
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

    # --- payouts (A3 slice 2): banks, account resolution, transfer recipients and transfers -------------------------------

    def _call(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
              json_body: Optional[Dict[str, Any]] = None) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        """One HTTP call to Paystack: (http status, parsed body). (None, None) when no answer came back at all (timeout,
        connection error) or the body was not JSON. Tests replace this method with the fake in app/tests/fake_meta.py."""
        try:
            response = requests.request(method, f"{self.base_url}{path}", headers=self.headers, params=params,
                                        json=json_body, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException:
            return None, None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body if isinstance(body, dict) else None

    @staticmethod
    def _failure(code: Optional[int], body: Optional[Dict[str, Any]], default: str) -> Dict[str, Any]:
        """A failed call. ``ambiguous`` is true when Paystack may have acted anyway (no answer, a 5xx, a 429, or a reply we
        could not read): callers must not assume nothing happened. A 4xx is Paystack definitively refusing."""
        message = (body or {}).get("message") if isinstance(body, dict) else None
        definite = code is not None and 400 <= code < 500 and code != 429
        return {"status": False, "ambiguous": not definite, "http_status": code, "message": message or default}

    def list_banks(self) -> Dict[str, Any]:
        """Every Nigerian bank Paystack can pay: {"status": True, "banks": [{"name", "code", "slug"}]}."""
        banks: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _page in range(10):  # a hard stop: Nigeria has a few hundred banks, 100 a page
            params: Dict[str, Any] = {"country": "nigeria", "currency": "NGN", "perPage": 100, "use_cursor": "true"}
            if cursor:
                params["next"] = cursor
            code, body = self._call("GET", "/bank", params=params)
            if code != 200 or not body or not body.get("status") or not isinstance(body.get("data"), list):
                return self._failure(code, body, "Could not load the bank list")
            banks.extend({"name": b["name"], "code": str(b["code"]), "slug": b.get("slug"),
                          "id": str(b["id"]) if b.get("id") is not None else None}
                         for b in body["data"] if b.get("name") and b.get("code") and not b.get("is_deleted"))
            cursor = ((body.get("meta") or {}).get("next"))
            if not cursor:
                break
        return {"status": True, "banks": banks}

    def resolve_account(self, account_number: str, bank_code: str) -> Dict[str, Any]:
        """The name the bank holds for the account: {"status": True, "account_name", "account_number"}."""
        code, body = self._call("GET", "/bank/resolve", params={"account_number": account_number, "bank_code": bank_code})
        if code == 200 and body and body.get("status") and (body.get("data") or {}).get("account_name"):
            bank_id = body["data"].get("bank_id")
            return {"status": True, "account_name": body["data"]["account_name"],
                    "account_number": body["data"].get("account_number") or account_number,
                    "bank_id": str(bank_id) if bank_id is not None else None}
        return self._failure(code, body, "Could not resolve the account")

    def create_transfer_recipient(self, *, name: str, account_number: str, bank_code: str) -> Dict[str, Any]:
        """Register the account as a transfer recipient (Paystack returns the existing one for a known account)."""
        code, body = self._call("POST", "/transferrecipient", json_body={
            "type": "nuban", "name": name, "account_number": account_number, "bank_code": bank_code, "currency": "NGN"})
        if code in (200, 201) and body and body.get("status") and (body.get("data") or {}).get("recipient_code"):
            return {"status": True, "recipient_code": body["data"]["recipient_code"]}
        return self._failure(code, body, "Could not register the account for payouts")

    def initiate_transfer(self, *, amount_kobo: int, recipient_code: str, reference: str, reason: str) -> Dict[str, Any]:
        """Send money from the Paystack balance. ``reference`` is unique per attempt: a repeat of the same reference is
        refused by Paystack, never paid twice. Success carries Paystack's transfer ``status`` (``pending``, ``success`` or
        ``otp`` when the account still demands an OTP for API transfers) and the ``transfer_code``."""
        code, body = self._call("POST", "/transfer", json_body={
            "source": "balance", "amount": int(amount_kobo), "recipient": recipient_code, "reference": reference,
            "reason": reason, "currency": "NGN"})
        if code in (200, 201) and body and body.get("status") and isinstance(body.get("data"), dict):
            data = body["data"]
            return {"status": True, "transfer_status": data.get("status"), "transfer_code": data.get("transfer_code"),
                    "message": body.get("message")}
        failure = self._failure(code, body, "Transfer was not accepted")
        if code in (408, 409):
            # A timeout or a conflict says nothing about whether the transfer exists: leave it to the webhook / reconcile.
            failure["ambiguous"] = True
        return failure

    def verify_transfer(self, reference: str) -> Dict[str, Any]:
        """Where a transfer stands: {"status": True, "transfer_status", "transfer_code", "amount_kobo"}. A 404 means Paystack
        has no transfer with that reference (``not_found``)."""
        code, body = self._call("GET", f"/transfer/verify/{reference}")
        if code == 200 and body and body.get("status") and isinstance(body.get("data"), dict):
            data = body["data"]
            return {"status": True, "transfer_status": data.get("status"), "transfer_code": data.get("transfer_code"),
                    "amount_kobo": data.get("amount"), "reason": data.get("reason")}
        result = self._failure(code, body, "Could not check the transfer")
        result["not_found"] = code == 404
        return result

    # --- refunds v2: create / fetch / list / retry ----------------------------------------------------------------------

    @staticmethod
    def _refund_write_result(code: Optional[int], body: Optional[Dict[str, Any]], default: str) -> Dict[str, Any]:
        """Classify a refund write. ``accepted``: Paystack queued it. ``refused``: a definite 4xx (nothing was created).
        ``unknown``: no answer, a 5xx / 429, or a body we could not read, so a refund may exist and callers must look it up."""
        message = body.get("message") if isinstance(body, dict) else None
        data = body.get("data") if isinstance(body, dict) else None
        if code in (200, 201) and isinstance(body, dict) and body.get("status") and isinstance(data, dict) \
                and data.get("id") is not None:
            return {"outcome": "accepted", "paystack_refund_id": str(data["id"]), "status": data.get("status"),
                    "message": message}
        # Only a 4xx whose body Paystack itself sent (a parsed JSON object) is a refusal: a proxy's HTML 4xx may never have
        # reached Paystack, or may have, so it is unknown.
        if code is not None and 400 <= code < 500 and code != 429 and isinstance(body, dict):
            return {"outcome": "refused", "paystack_refund_id": None, "status": None, "message": message or default}
        return {"outcome": "unknown", "paystack_refund_id": None, "status": None, "message": message or default}

    def create_refund(self, transaction_reference: str, amount_kobo: int) -> Dict[str, Any]:
        """Ask Paystack to refund ``amount_kobo`` of a settled booking payment. A timeout or 5xx is ``unknown``, never
        ``refused``: Paystack may have queued the refund, so the caller must look it up (list_refunds) before retrying."""
        code, body = self._call("POST", "/refund", json_body={"transaction": transaction_reference,
                                                              "amount": int(amount_kobo), "currency": "NGN"})
        return self._refund_write_result(code, body, "Refund was not accepted")

    def fetch_refund(self, paystack_refund_id: str) -> Dict[str, Any]:
        """Where one refund stands: ``found`` (with status and amount), ``not_found`` (a 404) or ``unknown``."""
        code, body = self._call("GET", f"/refund/{paystack_refund_id}")
        message = body.get("message") if isinstance(body, dict) else None
        data = body.get("data") if isinstance(body, dict) else None
        if code == 200 and isinstance(body, dict) and body.get("status") and isinstance(data, dict):
            return {"outcome": "found", "status": data.get("status"), "amount_kobo": data.get("amount"), "message": message}
        outcome = "not_found" if code == 404 else "unknown"
        return {"outcome": outcome, "status": None, "amount_kobo": None, "message": message}

    def list_refunds(self, transaction_reference: str) -> Dict[str, Any]:
        """Every refund Paystack holds for a transaction: {"outcome": "found"|"unknown", "refunds": [...]}."""
        code, body = self._call("GET", "/refund", params={"transaction": transaction_reference})
        if code == 200 and isinstance(body, dict) and body.get("status") and isinstance(body.get("data"), list):
            return {"outcome": "found", "refunds": [
                {"paystack_refund_id": str(r["id"]), "status": r.get("status"), "amount_kobo": r.get("amount")}
                for r in body["data"] if isinstance(r, dict) and r.get("id") is not None]}
        return {"outcome": "unknown", "refunds": []}

    def retry_refund_with_customer_details(self, paystack_refund_id: str, account_number: str, bank_id: str) -> Dict[str, Any]:
        """Give Paystack the customer's bank account for a refund in ``needs-attention``. Paystack answers 422 (``refused``)
        for a refund in any other state."""
        code, body = self._call("POST", f"/refund/retry_with_customer_details/{paystack_refund_id}", json_body={
            "refund_account_details": {"currency": "NGN", "account_number": account_number, "bank_id": bank_id}})
        return self._refund_write_result(code, body, "Refund retry was not accepted")
