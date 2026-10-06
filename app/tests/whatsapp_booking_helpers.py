"""Shared setup for the W2 booking tests: prices, the Flow key, a Flow driver and a chat that books."""

import json
from datetime import timedelta
from typing import Optional

from app.channels.whatsapp import booking_rules as rules
from app.tests.booking_fixtures import booking_env, make_prices  # noqa: F401
from app.tests.fake_meta import (  # noqa: F401
    FlowClient, body_bytes, fake_meta, fake_paystack, flow_reply_payload, flow_request, signed_headers,
)
from app.tests.test_whatsapp_processor import Chat, onboarded  # noqa: F401

CONDITION = "post-surgery wound dressing, SENTINEL-CONDITION-4471"
INSTRUCTIONS = "gate code is SENTINEL-NOTE-9921"

class FlowDriver:
    """Plays the part of Meta's Flow runtime: signed, encrypted requests to the data endpoint."""

    def __init__(self, client, public_pem: str, token: str):
        self.client, self.crypto, self.token = client, FlowClient(public_pem), token

    def call(self, action: str, screen: Optional[str] = None, data: Optional[dict] = None, *, token: Optional[str] = None,
             sign_with: Optional[str] = None, tamper=None):
        body = self.crypto.encrypt(flow_request(action, screen, data, token or self.token))
        if tamper:
            tamper(body)
        raw = body_bytes(body)
        headers = signed_headers(raw, sign_with) if sign_with else signed_headers(raw)
        response = self.client.post("/api/v1/whatsapp/flows/data", content=raw, headers=headers)
        if response.status_code == 200 and response.text:
            return response.status_code, self.crypto.decrypt_response(response.text)
        return response.status_code, None

    def exchange(self, screen: str, data: dict):
        return self.call("data_exchange", screen, data)

    def fill(self, *, who="self", service="nurse", day=None, time="10:00", hours=4, condition=CONDITION,
             instructions=INSTRUCTIONS, street="12 Test Close", area="Lekki Phase 1", city="Lagos", recipient=None):
        """Walk every screen the way the Flow does and return the last response (SUCCESS when it all went well)."""
        day = day or (rules.today_lagos(rules_now()) + timedelta(days=3))
        status, body = self.call("INIT")
        assert status == 200 and body["screen"] == "WHO", (status, body)
        status, body = self.exchange("WHO", {"who": who})
        if who == "new":
            assert body["screen"] == "RECIPIENT", body
            details = {"full_name": "Folake Adeyemi", "relationship": "parent", "age": "67", "gender": "female",
                       "emergency_phone": ""}
            details.update(recipient or {})
            status, body = self.exchange("RECIPIENT", details)
        assert body["screen"] == "SERVICE", body
        status, body = self.exchange("SERVICE", {"service": service})
        assert body["screen"] == "WHEN", body
        status, body = self.exchange("WHEN", {"trigger": "submit", "date": day.isoformat(), "time": time})
        assert body["screen"] == "DETAILS", body
        status, body = self.exchange("DETAILS", {"duration": str(hours), "condition": condition,
                                                 "instructions": instructions})
        assert body["screen"] == "ADDRESS", body
        return self.exchange("ADDRESS", {"street": street, "area": area, "city": city})


def rules_now():
    from datetime import datetime
    return datetime.utcnow()


def flow_token_from(bodies) -> str:
    """The flow token in the most recent Flow message the bot sent."""
    for body in reversed(bodies):
        interactive = body.get("interactive") or {}
        if interactive.get("type") == "flow":
            return interactive["action"]["parameters"]["flow_token"]
    raise AssertionError("the bot has not sent a Flow")


def all_text(bodies) -> str:
    return json.dumps(bodies)
