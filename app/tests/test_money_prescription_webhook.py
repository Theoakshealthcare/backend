"""Prescription payments through the Paystack webhook: the right amount marks it paid (replay-safe), a wrong amount does not,
and a late failure never un-pays it."""

import hashlib
import hmac
import json
import uuid

from app.core.config import settings
from app.core.money import to_kobo
from app.db.models.prescription import Prescription
from app.tests.factories import make_patient

WEBHOOK = "/api/v1/payment/paystack/webhook"


def _event(client, event, data):
    body = json.dumps({"event": event, "data": data}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    return client.post(WEBHOOK, content=body, headers={"x-paystack-signature": sig})


def _rx(db, cost=2500.0, status="pending"):
    patient = make_patient(db)
    rx = Prescription(patient_id=patient.id, requested_by_user_id=patient.user_id, medication_name="X",
                      delivery_option="Pickup", address="1 Test Close", total_cost_kobo=to_kobo(cost),
                      paystack_reference=f"RX_{uuid.uuid4().hex[:8]}", payment_status=status)
    db.add(rx)
    db.commit()
    return rx


def _rx_status(db, rx):
    db.expire_all()
    return db.get(Prescription, rx.id).payment_status


def test_prescription_paid_with_correct_amount_and_replay_safe(db, client):
    rx = _rx(db)
    for _ in range(2):
        assert _event(client, "charge.success",
                      {"reference": rx.paystack_reference, "amount": 250000, "status": "success"}).status_code == 200
    assert _rx_status(db, rx) == "paid"


def test_prescription_wrong_amount_not_marked_paid(db, client):
    rx = _rx(db)
    _event(client, "charge.success", {"reference": rx.paystack_reference, "amount": 100, "status": "success"})
    assert _rx_status(db, rx) == "pending"


def test_prescription_failed_after_paid_stays_paid(db, client):
    rx = _rx(db, status="paid")
    _event(client, "charge.failed", {"reference": rx.paystack_reference, "amount": 250000, "status": "failed"})
    assert _rx_status(db, rx) == "paid"
