"""W2: the booking Flow's data endpoint, through the real route (signature, decryption, screens, validation)."""

import json
import time
from datetime import timedelta

import pytest

from app.channels.whatsapp import booking_rules as rules
from app.channels.whatsapp.flows import token as flow_token
from app.core.config import settings
from app.core.security import decrypt
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.whatsapp import WhatsAppContact
from app.db.models.whatsapp_booking import Quote
from app.tests.whatsapp_booking_helpers import (  # noqa: F401  (fixtures)
    CONDITION, INSTRUCTIONS, Chat, FlowDriver, flow_token_from, onboarded, rules_now,
)


@pytest.fixture
def chat(db, fake_meta):
    return Chat(db, fake_meta)


@pytest.fixture
def flow(db, client, chat, booking_env):
    """A booked-in contact with an open draft, and a driver speaking to the endpoint with that draft's token."""
    onboarded(chat)
    chat.pick("menu:book")
    return FlowDriver(client, booking_env.public, flow_token_from(chat.fake.bodies))


def _quote(db):
    db.expire_all()
    return db.query(Quote).one()


# --- transport: signature, encryption, health check -----------------------------------------------
def test_ping_is_answered_encrypted(client, booking_env):
    status, body = FlowDriver(client, booking_env.public, "ignored").call("ping")
    assert status == 200 and body == {"version": "3.0", "data": {"status": "active"}}


def test_a_wrong_signature_is_432_and_nothing_is_decrypted(client, booking_env):
    driver = FlowDriver(client, booking_env.public, "ignored")
    status, body = driver.call("ping", sign_with="not-the-app-secret")
    assert (status, body) == (432, None)


def test_a_missing_signature_is_432(client, booking_env):
    driver = FlowDriver(client, booking_env.public, "x")
    raw = json.dumps(driver.crypto.encrypt({"action": "ping"})).encode()
    assert client.post("/api/v1/whatsapp/flows/data", content=raw).status_code == 432


def test_with_no_app_secret_everything_is_refused(client, booking_env, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    assert FlowDriver(client, booking_env.public, "x").call("ping", sign_with="")[0] == 432


def test_a_tampered_body_is_421_even_when_the_signature_is_valid(client, booking_env):
    """The signature protects the transport; the GCM tag protects the payload. Either failing refuses."""
    def flip(body):
        import base64
        raw = bytearray(base64.b64decode(body["encrypted_flow_data"]))
        raw[3] ^= 1
        body["encrypted_flow_data"] = base64.b64encode(bytes(raw)).decode()

    status, body = FlowDriver(client, booking_env.public, "x").call("ping", tamper=flip)
    assert (status, body) == (421, None)


def test_a_body_that_is_not_json_is_421(client, booking_env):
    from app.tests.fake_meta import signed_headers
    raw = b"not json at all"
    assert client.post("/api/v1/whatsapp/flows/data", content=raw, headers=signed_headers(raw)).status_code == 421


def test_without_a_configured_private_key_the_endpoint_is_503_not_open(client, booking_env, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_FLOW_PRIVATE_KEY", "")
    assert FlowDriver(client, booking_env.public, "x").call("ping")[0] == 503


def test_the_response_is_not_readable_without_the_request_key(client, booking_env):
    driver = FlowDriver(client, booking_env.public, "x")
    body = driver.crypto.encrypt({"action": "ping"})
    raw = json.dumps(body).encode()
    from app.tests.fake_meta import signed_headers
    response = client.post("/api/v1/whatsapp/flows/data", content=raw, headers=signed_headers(raw))
    assert response.status_code == 200 and "active" not in response.text and "{" not in response.text


def test_the_oversize_body_is_refused(client, booking_env):
    from app.tests.fake_meta import signed_headers
    raw = b"x" * 1_100_000
    assert client.post("/api/v1/whatsapp/flows/data", content=raw, headers=signed_headers(raw)).status_code == 413


# --- the flow token -------------------------------------------------------------------------------
def test_a_forged_token_is_427(flow):
    assert flow.call("INIT", token="not-a-token")[0] == 427
    assert flow.call("INIT", token=flow.token[:-2] + "xx")[0] == 427


def test_an_expired_token_is_427(db, flow, monkeypatch):
    real = flow_token.verify
    monkeypatch.setattr(flow_token, "verify", lambda token, now=None: real(token, rules_now() + timedelta(hours=2)))
    assert flow.call("INIT")[0] == 427


def test_a_token_for_another_draft_cannot_be_used_after_the_draft_closes(db, flow):
    flow.fill()
    assert flow.call("INIT")[0] == 427  # the draft is submitted now: the same token cannot reopen it


def test_a_blocked_contact_cannot_use_the_flow(db, flow):
    contact = db.query(WhatsAppContact).one()
    contact.blocked = True
    db.commit()
    assert flow.call("INIT")[0] == 427


# --- the screens ----------------------------------------------------------------------------------
def test_init_opens_who_with_myself_and_someone_new(flow):
    status, body = flow.call("INIT")
    assert status == 200 and body["version"] == "3.0" and body["screen"] == "WHO"
    assert [r["id"] for r in body["data"]["recipients"]] == ["self", "new"]


def test_a_complete_booking_for_myself(db, flow):
    status, body = flow.fill()
    assert status == 200 and body["screen"] == "SUCCESS"
    assert body["data"]["extension_message_response"]["params"] == {"flow_token": flow.token}
    quote = _quote(db)
    assert quote.status == "submitted"
    assert quote.payload["service"] == "nurse" and quote.payload["duration_hours"] == 4
    assert quote.payload["city"] == "Lagos" and quote.payload["address"] == "12 Test Close, Lekki Phase 1"


def test_services_show_the_price_per_hour_and_only_priced_services(db, flow, booking_env):
    flow.call("INIT")
    _, body = flow.exchange("WHO", {"who": "self"})
    offered = {s["id"]: s["description"] for s in body["data"]["services"]}
    assert offered == {"nurse": "₦11,000 per hour", "caregiver": "₦8,800 per hour",
                       "physiotherapist": "₦13,200 per hour", "doctor": "₦22,000 per hour"}
    assert "nutritionist" not in offered  # no multiplier configured, so never offered


def test_a_service_without_a_multiplier_cannot_be_chosen_even_if_the_client_sends_it(flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    status, body = flow.exchange("SERVICE", {"service": "nutritionist"})
    assert body["screen"] == "SERVICE" and body["data"]["notice"]


def test_a_service_row_without_any_multiplier_ends_the_flow_instead_of_an_empty_list(db, flow, booking_env):
    booking_env.prices.multipliers = {}
    db.commit()
    flow.call("INIT")
    status, body = flow.exchange("WHO", {"who": "self"})
    assert body["screen"] == "SUCCESS" and _quote(db).status == "cancelled"


def test_the_calendar_offers_today_to_thirty_days_and_slots_follow_the_chosen_date(db, flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    _, body = flow.exchange("SERVICE", {"service": "nurse"})
    data = body["data"]
    first, last = rules.date_range(rules_now())
    assert (data["min_date"], data["max_date"]) == (first.isoformat(), last.isoformat())
    far = (first + timedelta(days=10)).isoformat()
    _, body = flow.exchange("WHEN", {"trigger": "date_selected", "date": far})
    assert body["screen"] == "WHEN" and body["data"]["first_date"] == far
    assert [s["id"] for s in body["data"]["slots"]][0] == "07:00" and body["data"]["slots"][0]["title"] == "7:00 AM"


def test_a_date_outside_the_window_is_refused(flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "nurse"})
    too_far = (rules.today_lagos(rules_now()) + timedelta(days=31)).isoformat()
    _, body = flow.exchange("WHEN", {"trigger": "submit", "date": too_far, "time": "10:00"})
    assert body["screen"] == "WHEN" and body["data"]["notice"]
    yesterday = (rules.today_lagos(rules_now()) - timedelta(days=1)).isoformat()
    _, body = flow.exchange("WHEN", {"trigger": "submit", "date": yesterday, "time": "10:00"})
    assert body["screen"] == "WHEN"


def test_a_time_that_is_not_offered_is_refused(flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "nurse"})
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    for bad in ("03:00", "10:30", "bogus", None, 10):
        _, body = flow.exchange("WHEN", {"trigger": "submit", "date": day, "time": bad})
        assert body["screen"] == "WHEN" and body["data"]["notice"], bad


def test_duration_is_capped_per_service(db, flow, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_SERVICE_MAX_HOURS", "nurse:6,caregiver:24")
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "nurse"})
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    _, body = flow.exchange("WHEN", {"trigger": "submit", "date": day, "time": "10:00"})
    assert [d["id"] for d in body["data"]["durations"]] == ["1", "2", "3", "4", "5", "6"]
    _, body = flow.exchange("DETAILS", {"duration": "7", "condition": "x", "instructions": ""})
    assert body["screen"] == "DETAILS" and body["data"]["notice"]
    _, body = flow.exchange("DETAILS", {"duration": "0", "condition": "x", "instructions": ""})
    assert body["screen"] == "DETAILS"


def test_the_condition_is_limited_to_500_characters_and_required(flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "nurse"})
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    flow.exchange("WHEN", {"trigger": "submit", "date": day, "time": "10:00"})
    _, body = flow.exchange("DETAILS", {"duration": "2", "condition": "x" * 501, "instructions": ""})
    assert body["screen"] == "DETAILS" and body["data"]["notice"]
    _, body = flow.exchange("DETAILS", {"duration": "2", "condition": "   ", "instructions": ""})
    assert body["screen"] == "DETAILS"
    _, body = flow.exchange("DETAILS", {"duration": "2", "condition": "x" * 500, "instructions": ""})
    assert body["screen"] == "ADDRESS"


# --- privacy of the condition ------------------------------------------------------------------------
def test_the_condition_is_stored_encrypted_and_never_returned(db, flow):
    responses = []
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "nurse"})
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    flow.exchange("WHEN", {"trigger": "submit", "date": day, "time": "10:00"})
    _, details = flow.exchange("DETAILS", {"duration": "2", "condition": CONDITION, "instructions": INSTRUCTIONS})
    responses.append(details)
    _, final = flow.exchange("ADDRESS", {"street": "1 Test Rd", "area": "Yaba", "city": "Lagos"})
    responses.append(final)
    assert "SENTINEL" not in json.dumps(responses)
    quote = _quote(db)
    assert quote.sensitive_encrypted and "SENTINEL" not in quote.sensitive_encrypted
    assert "SENTINEL" not in json.dumps(quote.payload)
    assert json.loads(decrypt(quote.sensitive_encrypted)) == {"condition": CONDITION, "instructions": INSTRUCTIONS}


def test_the_condition_does_not_reach_the_logs(db, flow, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    flow.fill()
    assert "SENTINEL" not in caplog.text


# --- recipients ---------------------------------------------------------------------------------
def test_someone_new_is_saved_as_a_recipient_with_logistics_access_only(db, flow):
    status, body = flow.fill(who="new", recipient={"full_name": "Folake Adeyemi", "age": "67"})
    assert body["screen"] == "SUCCESS"
    contact = db.query(WhatsAppContact).one()
    rows = (db.query(CareRelationship, CareRecipient).join(CareRecipient, CareRecipient.id == CareRelationship.recipient_id)
            .filter(CareRelationship.contact_user_id == contact.user_id, CareRelationship.role == "family").all())
    assert len(rows) == 1
    relationship, recipient = rows[0]
    assert recipient.full_name == "Folake Adeyemi" and relationship.clinical_access is False
    assert recipient.date_of_birth.year == rules.today_lagos(rules_now()).year - 67
    assert recipient.created_by_user_id == contact.user_id
    assert str(recipient.id) == _quote(db).payload["recipient_id"]


def test_a_saved_recipient_is_offered_next_time(db, flow, chat, client, booking_env):
    flow.fill(who="new")
    chat.say("cancel")  # leave the flow; the recipient stays saved
    chat.pick("menu:book")
    second = FlowDriver(client, booking_env.public, flow_token_from(chat.fake.bodies))
    _, body = second.call("INIT")
    ids = [r["id"] for r in body["data"]["recipients"]]
    assert ids[0] == "self" and ids[1].startswith("saved:") and ids[-1] == "new"
    assert body["data"]["recipients"][1]["title"] == "Folake Adeyemi"


def test_a_recipient_id_that_is_not_yours_is_refused(db, flow):
    from app.tests.factories import make_user
    stranger = make_user(db, "patient")
    other = CareRecipient(full_name="Somebody Else", created_by_user_id=stranger.id)
    db.add(other)
    db.flush()
    db.add(CareRelationship(contact_user_id=stranger.id, recipient_id=other.id, role="family"))
    db.commit()
    flow.call("INIT")
    _, body = flow.exchange("WHO", {"who": f"saved:{other.id}"})
    assert body["screen"] == "WHO" and body["data"]["notice"]
    _, body = flow.exchange("WHO", {"who": "saved:not-a-uuid"})
    assert body["screen"] == "WHO"


def test_ten_saved_recipients_is_the_limit(db, flow):
    contact = db.query(WhatsAppContact).one()
    for i in range(settings.WHATSAPP_MAX_RECIPIENTS):
        recipient = CareRecipient(full_name=f"Person {i}", created_by_user_id=contact.user_id)
        db.add(recipient)
        db.flush()
        db.add(CareRelationship(contact_user_id=contact.user_id, recipient_id=recipient.id, role="family"))
    db.commit()
    _, body = flow.call("INIT")
    assert "new" not in [r["id"] for r in body["data"]["recipients"]]
    _, body = flow.exchange("WHO", {"who": "new"})
    assert body["screen"] == "WHO" and body["data"]["notice"]  # a hand-made request cannot get round it


@pytest.mark.parametrize("override,fragment", [
    ({"full_name": "x"}, "name"), ({"full_name": "12345"}, "name"), ({"full_name": ""}, "name"),
    ({"age": "200"}, "age"), ({"age": "abc"}, "age"), ({"age": "-1"}, "age"),
    ({"gender": "robot"}, "gender"), ({"relationship": "enemy"}, "relationship"),
    ({"emergency_phone": "12"}, "emergency"),
])
def test_recipient_details_are_validated_again_on_the_server(flow, override, fragment):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "new"})
    details = {"full_name": "Folake Adeyemi", "relationship": "parent", "age": "67", "gender": "female",
               "emergency_phone": ""}
    details.update(override)
    _, body = flow.exchange("RECIPIENT", details)
    assert body["screen"] == "RECIPIENT" and fragment in body["data"]["notice"].lower()


# --- address and service area --------------------------------------------------------------------
def test_abuja_is_served_and_a_city_name_is_matched_case_insensitively(db, flow):
    _, body = flow.fill(city="abuja")
    assert body["screen"] == "SUCCESS"
    assert _quote(db).payload["city"] == "Abuja" and _quote(db).payload["state"] == "FCT"


def test_a_city_outside_the_service_area_goes_to_the_waitlist_and_drops_the_details(db, flow):
    _, body = flow.fill(city="other")
    assert body["screen"] == "SUCCESS"  # the Flow ends normally; the chat then offers the waitlist
    quote = _quote(db)
    assert quote.status == "out_of_area" and quote.sensitive_encrypted is None
    assert "address" not in quote.payload and "recipient_id" not in quote.payload
    assert "SENTINEL" not in json.dumps(quote.payload)


def test_an_unlisted_city_typed_by_a_client_is_also_out_of_area(db, flow):
    flow.fill(city="Port Harcourt")
    assert _quote(db).status == "out_of_area" and _quote(db).payload["city"] == "Port Harcourt"


def test_the_service_cities_are_a_setting(db, flow, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_SERVICE_CITIES", "Lagos")
    flow.fill(city="Abuja")
    assert _quote(db).status == "out_of_area"


def test_the_address_needs_a_street_and_an_area(flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "nurse"})
    day = (rules.today_lagos(rules_now()) + timedelta(days=2)).isoformat()
    flow.exchange("WHEN", {"trigger": "submit", "date": day, "time": "10:00"})
    flow.exchange("DETAILS", {"duration": "2", "condition": "needs help", "instructions": ""})
    for bad in ({"street": "", "area": "Yaba"}, {"street": "1 Rd", "area": ""}, {"street": "x" * 200, "area": "Yaba"}):
        _, body = flow.exchange("ADDRESS", {**bad, "city": "Lagos"})
        assert body["screen"] == "ADDRESS" and body["data"]["notice"]


def test_skipping_screens_cannot_complete_a_booking(db, flow):
    flow.call("INIT")
    _, body = flow.exchange("ADDRESS", {"street": "1 Rd", "area": "Yaba", "city": "Lagos"})
    assert body["screen"] == "WHO"  # missing answers send the person back to the start
    assert _quote(db).status == "draft"


def test_unknown_screens_and_actions_are_400(flow):
    assert flow.call("data_exchange", "NOPE", {})[0] == 400
    assert flow.call("explode")[0] == 400


def test_the_error_notification_from_meta_is_acknowledged(flow):
    status, body = flow.exchange("WHO", {"error": "invalid-screen-transition", "error_message": "x"})
    assert status == 200 and body["data"] == {"acknowledged": True}


# --- BACK and re-entry ----------------------------------------------------------------------------
def test_back_renders_the_screen_again_without_losing_answers(db, flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "caregiver"})
    status, body = flow.call("BACK", "SERVICE")
    assert status == 200 and body["screen"] == "SERVICE" and body["data"]["services"]
    assert _quote(db).payload["service"] == "caregiver"
    status, body = flow.call("BACK", "WHEN")
    assert body["screen"] == "WHEN" and body["data"]["slots"]


def test_choosing_again_overwrites_the_earlier_answer(db, flow):
    flow.call("INIT")
    flow.exchange("WHO", {"who": "self"})
    flow.exchange("SERVICE", {"service": "caregiver"})
    flow.exchange("SERVICE", {"service": "physiotherapist"})
    assert _quote(db).payload["service"] == "physiotherapist"


# --- speed -------------------------------------------------------------------------------------------
def test_screen_requests_are_fast(db, flow):
    """WA-FR-16 asks for p95 under 3 s; Meta's limit is 10 s. Locally each screen takes a few ms of work."""
    flow.call("INIT")
    samples = []
    for _ in range(40):
        started = time.perf_counter()
        flow.exchange("WHO", {"who": "self"})
        samples.append(time.perf_counter() - started)
    samples.sort()
    assert samples[int(len(samples) * 0.95)] < 1.0  # generous locally: it includes RSA-OAEP and AES in the test client
