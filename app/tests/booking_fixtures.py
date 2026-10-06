"""Fixtures for the W2 booking tests: registered in conftest so every test module sees them."""

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.db.models.booking_settings import BookingSettings
from app.tests.fake_meta import fake_meta, fake_paystack, make_flow_keypair  # noqa: F401

_KEYS = {}


def flow_keys():
    if not _KEYS:
        _KEYS["private"], _KEYS["public"] = make_flow_keypair()
    return _KEYS["private"], _KEYS["public"]


def make_prices(db, **overrides) -> BookingSettings:
    fields = dict(base_fee_kobo=1_000_000, platform_fee_percent=10.0, surge_pricing=False, surge_multiplier=1.0,
                  discount=0.0, multipliers={"nurse": 1.0, "caregiver": 0.8, "physiotherapist": 1.2, "doctor": 2.0})
    fields.update(overrides)
    row = BookingSettings(**fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def booking_env(db, monkeypatch, fake_meta, fake_paystack):
    """Booking in chat switched on: a Flow id, a Flow key, a price list, the fake Meta and the fake Paystack."""
    private, public = flow_keys()
    monkeypatch.setattr(settings, "WHATSAPP_BOOKING_FLOW_ID", "flow-test-1")
    monkeypatch.setattr(settings, "WHATSAPP_FLOW_PRIVATE_KEY", private)
    prices = make_prices(db)
    return SimpleNamespace(prices=prices, public=public, private=private, paystack=fake_paystack, meta=fake_meta)
