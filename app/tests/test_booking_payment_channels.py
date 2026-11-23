"""The optional Paystack ``channels`` choice (Card / Bank Transfer) on the booking payment link.

Money path, so the tests pin three things: (1) with no ``channels`` the Paystack initialise payload is exactly what it was
before the feature (same keys, same order, same bytes); (2) ``channels`` is forwarded only when asked for, only from the
allow-list, and never changes the amount, currency, reference or metadata; (3) a booking keeps the link it was first made
with, so a later request that names other channels gets that same link (Paystack cannot change the channels of a
transaction it already initialised, and a second initialise of one reference is refused anyway).
"""

import json
from datetime import datetime, timedelta

import pytest

from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.booking_settings import BookingSettings
from app.schemas.booking import BookingCreate
from app.services import dispatch
from app.services.booking import BookingService
from app.services.paystack import ALLOWED_CHECKOUT_CHANNELS, PaystackService
from app.tests.factories import auth_headers, make_booking, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)

API = "/api/v1/booking"
CREATE = dict(service_type="nurse", assistance_type="x", duration_hours=3, scheduled_date="2030-01-01T10:00:00",
              patient_address="1 Test Close", patient_latitude=6.4, patient_longitude=3.4, patient_city="Lagos",
              patient_state="Lagos", booking_for="myself")

# What the real service sent before channels existed: keys in this order, nothing else.
PRE_CHANNELS_KEYS = ["email", "amount", "reference", "currency", "callback_url", "metadata"]


@pytest.fixture(autouse=True)
def started(monkeypatch):
    monkeypatch.setattr(dispatch, "start_dispatch", lambda db, booking_id, now=None: None)


@pytest.fixture
def patient(db):
    return make_patient(db)


@pytest.fixture
def pricing(db):
    db.add(BookingSettings(base_fee_kobo=123457, platform_fee_percent=7.5, multipliers={"nurse": 1.3}, discount=3.3))
    db.commit()


@pytest.fixture
def real_like(fake_paystack):
    fake_paystack.reject_duplicate_reference = True  # what the real Paystack does
    return fake_paystack


@pytest.fixture
def wire(monkeypatch):
    """The real ``PaystackService.initialize_transaction`` with only ``requests.post`` replaced: the exact payload it sends."""
    sent = []

    class Resp:
        def raise_for_status(self): ...

        def json(self):
            return {"status": True, "data": {"authorization_url": "https://checkout.paystack.com/x", "access_code": "x",
                                             "reference": "r"}}

    def post(url, **kwargs):
        sent.append({"url": url, "json": kwargs["json"], "wire": json.dumps(kwargs["json"])})
        return Resp()

    monkeypatch.setattr("app.services.paystack.requests.post", post)
    return sent


def create(client, patient, **extra):
    return client.post(f"{API}/create", json={**CREATE, **extra}, headers=auth_headers(patient.user))


def link(client, patient, booking, **kwargs):
    return client.post(f"{API}/{booking.id}/payment-link", headers=auth_headers(patient.user), **kwargs)


def reload(db, booking) -> Booking:
    db.expire_all()
    return db.get(Booking, booking.id)


def code(r) -> str:
    return r.json()["detail"]["code"]


# --- the real service: what goes over the wire --------------------------------------------------------------------------
def test_without_channels_the_initialise_payload_is_exactly_what_it_was(wire):
    PaystackService().initialize_transaction(
        "p@example.com", 5_500_000, "BKG_REF", callback_url="https://app.example/cb", metadata={"booking_id": "b1"})
    (call,) = wire
    assert call["url"].endswith("/transaction/initialize")
    assert list(call["json"]) == PRE_CHANNELS_KEYS
    assert call["wire"] == ('{"email": "p@example.com", "amount": 5500000, "reference": "BKG_REF", "currency": "NGN", '
                            '"callback_url": "https://app.example/cb", "metadata": {"booking_id": "b1"}}')


def test_channels_none_is_the_same_as_leaving_it_out(wire):
    svc = PaystackService()
    svc.initialize_transaction("p@example.com", 100, "R", callback_url="u", metadata={"a": 1})
    svc.initialize_transaction("p@example.com", 100, "R", callback_url="u", metadata={"a": 1}, channels=None)
    assert wire[0]["wire"] == wire[1]["wire"]


def test_with_channels_only_that_one_key_is_added(wire):
    svc = PaystackService()
    svc.initialize_transaction("p@example.com", 100, "R", callback_url="u", metadata={"a": 1})
    svc.initialize_transaction("p@example.com", 100, "R", callback_url="u", metadata={"a": 1}, channels=["card", "bank_transfer"])
    plain, chosen = wire[0]["json"], wire[1]["json"]
    assert chosen == {**plain, "channels": ["card", "bank_transfer"]}
    assert list(chosen) == PRE_CHANNELS_KEYS + ["channels"]
    assert chosen["amount"] == 100 and chosen["currency"] == "NGN"


@pytest.mark.parametrize("bad", [["ussd"], ["Card"], ["bank"], [], ["card", "card"], ["card", "bank_transfer", "card"],
                                 ["card", "bank_transfer", "qr"], [None], ["card", 1]])
def test_the_service_refuses_anything_outside_the_allow_list_and_never_calls_paystack(wire, bad):
    with pytest.raises(ValueError):
        PaystackService().initialize_transaction("p@example.com", 100, "R", channels=bad)
    assert wire == []


def test_the_allow_list_is_card_and_bank_transfer_only():
    assert ALLOWED_CHECKOUT_CHANNELS == ("card", "bank_transfer")


# --- POST /booking/create ------------------------------------------------------------------------------------------------
def test_create_without_channels_sends_paystack_no_channels_key(db, client, patient, pricing, real_like):
    r = create(client, patient)
    assert r.status_code == 200, r.text
    (sent,) = real_like.initialised
    assert set(sent) == {"email", "amount", "reference", "callback_url", "metadata"}      # the fake's payload: no channels
    assert r.json()["payment_url"].startswith("https://checkout.paystack.com/")


def test_create_with_explicit_null_channels_is_the_same_as_none(db, client, patient, pricing, real_like):
    r = create(client, patient, channels=None)
    assert r.status_code == 200, r.text
    assert "channels" not in real_like.initialised[0]


@pytest.mark.parametrize("asked", [["card"], ["bank_transfer"], ["card", "bank_transfer"], ["bank_transfer", "card"]])
def test_create_forwards_the_choice_and_keeps_the_money_server_side(db, client, patient, pricing, real_like, asked):
    r = create(client, patient, channels=asked)
    assert r.status_code == 200, r.text
    b = db.query(Booking).one()
    (sent,) = real_like.initialised
    assert sent["channels"] == asked                                   # as given, order kept
    assert sent["amount"] == b.total_amount_kobo and sent["reference"] == b.paystack_reference
    assert sent["metadata"] == {"booking_id": str(b.id), "patient_id": str(b.patient_id), "service_type": "nurse",
                                "booking_code": b.booking_code}
    assert r.json()["payment_url"] == b.paystack_authorization_url


def test_create_ignores_an_amount_or_card_data_sent_by_the_client(db, client, patient, pricing, real_like):
    r = create(client, patient, channels=["card"], amount=1, amount_kobo=1, total_amount_kobo=1, currency="USD",
               reference="MINE", authorization_code="AUTH_x", card_number="4084084084084081")
    assert r.status_code == 200, r.text
    b = db.query(Booking).one()
    (sent,) = real_like.initialised
    assert sent["amount"] == b.total_amount_kobo != 1 and sent["reference"] == b.paystack_reference != "MINE"
    assert set(sent) == {"email", "amount", "reference", "callback_url", "metadata", "channels"}
    assert "AUTH_x" not in json.dumps(sent) and "4084" not in json.dumps(sent)


@pytest.mark.parametrize("bad", [["ussd"], ["Card"], ["bank"], [], ["card", "card"], ["card", "bank_transfer", "card"],
                                 ["card", "bank_transfer", "qr"], "card", [1], [None], {"card": True}, True, 0])
def test_create_refuses_a_bad_channels_value_with_422_and_creates_nothing(db, client, patient, pricing, real_like, bad):
    r = create(client, patient, channels=bad)
    assert r.status_code == 422, r.text
    assert any(e["loc"][-1] == "channels" or "channels" in e["loc"] for e in r.json()["detail"])
    assert db.query(Booking).count() == 0 and real_like.initialised == []


def test_create_422_names_the_field_and_the_rule(db, client, patient, pricing, real_like):
    dup = create(client, patient, channels=["card", "card"]).json()["detail"][0]
    assert dup["loc"] == ["body", "channels"] and "repeat" in dup["msg"]
    unknown = create(client, patient, channels=["ussd"]).json()["detail"][0]
    assert unknown["loc"] == ["body", "channels", 0]


# --- POST /booking/{id}/payment-link: the first link wins ---------------------------------------------------------------
def test_a_repeat_naming_other_channels_returns_the_first_link_and_asks_paystack_nothing_new(
        db, client, patient, pricing, real_like):
    created = create(client, patient, channels=["card"]).json()
    b = db.query(Booking).one()
    other = link(client, patient, b, json={"channels": ["bank_transfer"]})
    assert other.status_code == 200, other.text
    assert other.json()["payment_url"] == created["payment_url"]                 # the first link, not a new one
    assert other.json()["payment_access_code"] == created["payment_access_code"]
    assert len(real_like.initialised) == 1 and real_like.initialised[0]["channels"] == ["card"]
    assert reload(db, b).paystack_authorization_url == created["payment_url"] and db.query(Booking).count() == 1


def test_a_link_for_a_booking_made_without_channels_is_not_upgraded_by_a_later_request(db, client, patient, pricing, real_like):
    created = create(client, patient).json()
    b = db.query(Booking).one()
    r = link(client, patient, b, json={"channels": ["bank_transfer"]})
    assert r.status_code == 200 and r.json()["payment_url"] == created["payment_url"]
    assert len(real_like.initialised) == 1 and "channels" not in real_like.initialised[0]


@pytest.mark.parametrize("kwargs", [
    {},                                                                      # no body at all (every existing client)
    {"json": {}},
    {"json": {"channels": None}},
    {"content": b"", "headers": {"content-type": "application/json"}},      # an empty body that says it is JSON
])
def test_the_body_stays_optional_on_the_payment_link(db, client, patient, pricing, real_like, kwargs):
    created = create(client, patient).json()
    b = db.query(Booking).one()
    headers = {**auth_headers(patient.user), **kwargs.pop("headers", {})}
    r = client.post(f"{API}/{b.id}/payment-link", headers=headers, **kwargs)
    assert r.status_code == 200, r.text
    assert r.json()["payment_url"] == created["payment_url"]


@pytest.mark.parametrize("body", [{"channels": ["ussd"]}, {"channels": []}, {"channels": ["card", "card"]},
                                  {"channels": "card"}, {"amount": 100}, {"channels": ["card"], "amount_kobo": 1},
                                  {"card_number": "4084084084084081"}])
def test_the_payment_link_refuses_bad_channels_or_any_other_field_with_422_before_anything_else(
        db, client, patient, pricing, real_like, body):
    create(client, patient)
    b = db.query(Booking).one()
    before = (len(real_like.initialised), len(real_like.verified))
    r = link(client, patient, b, json=body)
    assert r.status_code == 422, r.text
    assert (len(real_like.initialised), len(real_like.verified)) == before       # Paystack was not asked anything
    assert reload(db, b).status == BookingStatus.PENDING


def _legacy(db, patient):
    """A pending booking with no stored link (made before migration 0023, or whose copy failed to save)."""
    return make_booking(db, patient, payment_status=PaymentStatus.PENDING,
                        payment_expires_at=datetime.utcnow() + timedelta(minutes=20))


def test_a_booking_with_no_stored_link_is_initialised_with_the_channels_asked_for_then_keeps_that_link(
        db, client, patient, real_like):
    b = _legacy(db, patient)
    first = link(client, patient, b, json={"channels": ["bank_transfer"]})
    assert first.status_code == 200, first.text
    (sent,) = real_like.initialised
    assert sent["channels"] == ["bank_transfer"] and sent["amount"] == b.total_amount_kobo
    again = link(client, patient, b, json={"channels": ["card"]})                # a different choice: the stored link wins
    assert again.json()["payment_url"] == first.json()["payment_url"]
    assert len(real_like.initialised) == 1


def test_a_booking_with_no_stored_link_and_no_channels_is_initialised_as_before(db, client, patient, real_like):
    b = _legacy(db, patient)
    assert link(client, patient, b).status_code == 200
    assert "channels" not in real_like.initialised[0]


# --- the other caller of start_payment (the WhatsApp channel) is untouched -----------------------------------------------
def test_start_payment_without_channels_sends_none(db, patient, pricing, real_like):
    svc = BookingService(db)
    b = svc.insert_booking(patient.id, BookingCreate(**CREATE), commit=True)
    svc.start_payment(b, callback_url="https://example.test/return")
    assert "channels" not in real_like.initialised[0]


def test_a_booking_create_object_defaults_to_no_channels():
    assert BookingCreate(**CREATE).channels is None
