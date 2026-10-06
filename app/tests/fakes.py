"""A fake Expo Push API for tests. Nothing here talks to the network: PushService is given an
httpx.MockTransport, so a push can never leave the machine."""

import json
import uuid
from typing import Callable, Optional

import httpx
import pytest

from app.core.config import settings
from app.services import push

EXPO_TEST_URL = "http://expo.test/--/api/v2/push/send"


class FakeExpo:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.batches: list[list[dict]] = []
        self.not_registered: set[str] = set()      # tokens Expo reports as DeviceNotRegistered
        self.other_errors: dict[str, str] = {}      # token -> some other Expo error code
        self.status_code = 200
        self.raise_error: Optional[Exception] = None
        self.on_request: Optional[Callable[[httpx.Request, list[dict]], None]] = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        batch = json.loads(request.content)
        self.batches.append(batch)
        if self.on_request:
            self.on_request(request, batch)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"errors": [{"code": "SERVER_ERROR"}]})
        tickets = []
        for msg in batch:
            token = msg["to"]
            if token in self.not_registered:
                tickets.append({"status": "error", "message": "not registered",
                                "details": {"error": "DeviceNotRegistered"}})
            elif token in self.other_errors:
                tickets.append({"status": "error", "message": "nope",
                                "details": {"error": self.other_errors[token]}})
            else:
                tickets.append({"status": "ok", "id": str(uuid.uuid4())})
        return httpx.Response(200, json={"data": tickets})

    @property
    def messages(self) -> list[dict]:
        return [m for batch in self.batches for m in batch]

    @property
    def raw_bodies(self) -> str:
        return "".join(r.content.decode() for r in self.requests)


@pytest.fixture
def expo(monkeypatch):
    fake = FakeExpo()
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(settings, "EXPO_PUSH_URL", EXPO_TEST_URL)
    return fake
