"""A production config without TRUSTED_PROXIES warns (behind a proxy all clients would share one IP)."""

import logging

from app.core.config import Settings
from app.tests.fake_prembly import PRODUCTION_KYC_KEYS


def _settings(**overrides):
    base = dict(ENVIRONMENT="production", PAYSTACK_SECRET_KEY="sk_test_placeholder", **PRODUCTION_KYC_KEYS)
    base.update(overrides)
    return Settings(**base)


def test_production_without_trusted_proxies_logs_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        _settings(TRUSTED_PROXIES=[])
    assert any("TRUSTED_PROXIES is empty" in r.getMessage() for r in caplog.records)


def test_production_with_trusted_proxies_is_quiet(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        _settings(TRUSTED_PROXIES=["10.0.0.0/8"])
    assert not any("TRUSTED_PROXIES is empty" in r.getMessage() for r in caplog.records)


def test_development_without_trusted_proxies_is_quiet(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        _settings(ENVIRONMENT="development", TRUSTED_PROXIES=[])
    assert not any("TRUSTED_PROXIES is empty" in r.getMessage() for r in caplog.records)
