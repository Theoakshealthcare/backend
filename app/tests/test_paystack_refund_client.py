"""Refunds v2, Task 2: the Paystack refund calls (create / fetch / list / retry), the fake that serves them, and recording
Paystack's fee when a booking payment settles. The client runs its real parsing against FakePaystack's replies."""

import hashlib
import hmac
import json

from app.core.config import settings
from app.db.models.booking import PaymentStatus
from app.services import dispatch
from app.services.paystack import PaystackService
from app.tests.factories import make_booking, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

REF = "REF_PLACEHOLDER_1"


def paid_transaction(fake, reference=REF, amount=4_400_000):
    fake.initialize_transaction(None, "placeholder@example.com", amount, reference)
    fake.paid.add(reference)


# --- create ----------------------------------------------------------------------------------------------------------


def test_create_refund_accepted_returns_id(fake_paystack):
    paid_transaction(fake_paystack)
    result = PaystackService().create_refund(REF, 1_000_000)
    assert result["outcome"] == "accepted"
    assert result["status"] == "pending"
    assert result["paystack_refund_id"] in fake_paystack.refunds
    assert fake_paystack.refunds[result["paystack_refund_id"]]["amount"] == 1_000_000


def test_create_refund_422_is_refused_with_message(fake_paystack):
    paid_transaction(fake_paystack)
    fake_paystack.refund_error = "422"
    result = PaystackService().create_refund(REF, 1_000_000)
    assert result["outcome"] == "refused"
    assert result["paystack_refund_id"] is None
    assert result["message"] == "Refund could not be processed"
    assert fake_paystack.refunds == {}


def test_create_refund_unknown_transaction_is_refused(fake_paystack):
    assert PaystackService().create_refund("NO_SUCH_REF", 100)["outcome"] == "refused"


def test_create_refund_timeout_is_unknown_never_refused(fake_paystack):
    paid_transaction(fake_paystack)
    fake_paystack.refund_error = "timeout"
    assert PaystackService().create_refund(REF, 1_000_000)["outcome"] == "unknown"
    fake_paystack.refund_error = "500"
    assert PaystackService().create_refund(REF, 1_000_000)["outcome"] == "unknown"


# --- fetch / list ----------------------------------------------------------------------------------------------------


def test_fetch_refund_found_and_not_found(fake_paystack):
    paid_transaction(fake_paystack)
    created = PaystackService().create_refund(REF, 2_000_000)
    found = PaystackService().fetch_refund(created["paystack_refund_id"])
    assert (found["outcome"], found["status"], found["amount_kobo"]) == ("found", "pending", 2_000_000)
    assert PaystackService().fetch_refund("999999")["outcome"] == "not_found"
    fake_paystack.refund_error = "timeout"
    assert PaystackService().fetch_refund(created["paystack_refund_id"])["outcome"] == "unknown"


def test_fetch_refund_not_found(fake_paystack):
    result = PaystackService().fetch_refund("424242")
    assert result["outcome"] == "not_found" and result["status"] is None and result["amount_kobo"] is None


def test_list_refunds_by_transaction(fake_paystack):
    paid_transaction(fake_paystack)
    paid_transaction(fake_paystack, reference="REF_PLACEHOLDER_2")
    a = PaystackService().create_refund(REF, 1_000_000)["paystack_refund_id"]
    b = PaystackService().create_refund(REF, 500_000)["paystack_refund_id"]
    PaystackService().create_refund("REF_PLACEHOLDER_2", 700_000)
    listed = PaystackService().list_refunds(REF)
    assert listed["outcome"] == "found"
    assert listed["refunds"] == [{"paystack_refund_id": a, "status": "pending", "amount_kobo": 1_000_000},
                                 {"paystack_refund_id": b, "status": "pending", "amount_kobo": 500_000}]
    assert PaystackService().list_refunds("NONE")["refunds"] == []
    fake_paystack.refund_error = "500"
    assert PaystackService().list_refunds(REF) == {"outcome": "unknown", "refunds": []}


# --- retry -----------------------------------------------------------------------------------------------------------


def test_retry_with_customer_details_only_after_needs_attention(fake_paystack):
    paid_transaction(fake_paystack)
    refund_id = PaystackService().create_refund(REF, 1_000_000)["paystack_refund_id"]
    early = PaystackService().retry_refund_with_customer_details(refund_id, "0123456789", "9")
    assert early["outcome"] == "refused"  # Paystack answers 422 unless the refund is needs-attention
    fake_paystack.settle_refund(refund_id, "needs-attention")
    ok = PaystackService().retry_refund_with_customer_details(refund_id, "0123456789", "9")
    assert ok["outcome"] == "accepted" and ok["status"] == "processing"
    method, path, _query, body = fake_paystack.requests[-1]
    assert (method, path) == ("POST", f"/refund/retry_with_customer_details/{refund_id}")
    assert body == {"refund_account_details": {"currency": "NGN", "account_number": "0123456789", "bank_id": "9"}}
    fake_paystack.refund_error = "timeout"
    assert PaystackService().retry_refund_with_customer_details(refund_id, "0123456789", "9")["outcome"] == "unknown"


def test_settle_refund_webhook_shape(fake_paystack):
    paid_transaction(fake_paystack)
    refund_id = PaystackService().create_refund(REF, 1_000_000)["paystack_refund_id"]
    body = fake_paystack.settle_refund(refund_id, "processed")
    assert body["event"] == "refund.processed"
    assert body["data"]["transaction_reference"] == REF
    assert body["data"]["refund_reference"] is None
    assert body["data"]["amount"] == "1000000"
    assert body["data"]["id"] == int(refund_id) and body["data"]["status"] == "processed"
    assert fake_paystack.refunds[refund_id]["status"] == "processed"


# --- the Paystack fee on settlement ----------------------------------------------------------------------------------


def _charge_success(client, booking, **data):
    body = json.dumps({"event": "charge.success", "data": {
        "reference": booking.paystack_reference, "amount": booking.total_amount_kobo, "status": "success", **data}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/v1/payment/paystack/webhook", content=body, headers={"x-paystack-signature": sig})


def test_charge_success_records_paystack_fee(db, client, monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    assert _charge_success(client, booking, fees=66000).status_code == 200
    db.expire_all()
    db.refresh(booking)
    assert booking.payment_status == PaymentStatus.PAID
    assert booking.paystack_fee_kobo == 66000


def test_charge_without_fees_leaves_fee_null(db, client, monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    assert _charge_success(client, booking).status_code == 200
    db.expire_all()
    db.refresh(booking)
    assert booking.payment_status == PaymentStatus.PAID
    assert booking.paystack_fee_kobo is None


def test_verify_path_records_paystack_fee(db, monkeypatch, fake_paystack):
    from app.services import booking_payment

    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)
    booking = make_booking(db, make_patient(db), payment_status=PaymentStatus.PENDING)
    fake_paystack.initialize_transaction(None, "placeholder@example.com", booking.total_amount_kobo, booking.paystack_reference)
    fake_paystack.paid.add(booking.paystack_reference)
    fake_paystack.fee_kobo = 66000
    assert booking_payment.verify_and_settle(db, booking.id) == booking_payment.SettleOutcome.PAID
    db.expire_all()
    db.refresh(booking)
    assert booking.paystack_fee_kobo == 66000


# --- an unreadable 4xx is unknown, never refused ---------------------------------------------------------------------


def test_unreadable_4xx_refund_reply_is_unknown(monkeypatch):
    for status in (400, 403, 404, 422):
        monkeypatch.setattr(PaystackService, "_call", lambda self, *a, _s=status, **kw: (_s, None))  # e.g. a proxy's HTML page
        assert PaystackService().create_refund(REF, 100)["outcome"] == "unknown"
        assert PaystackService().retry_refund_with_customer_details("1", "0123456789", "9")["outcome"] == "unknown"
