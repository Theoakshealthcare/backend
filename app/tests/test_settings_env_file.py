"""Settings must load from the shipped .env.example and accept every list format."""

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


@pytest.fixture
def required(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.setenv("VITALS_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_env_example_loads_as_an_env_file(required, monkeypatch):
    for name in ("TRUSTED_PROXIES", "BACKEND_CORS_ORIGINS", "ADMIN_LOCKOUT_TIERS_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=ENV_EXAMPLE)
    assert s.TRUSTED_PROXIES == []
    assert [str(u) for u in s.BACKEND_CORS_ORIGINS] == ["http://localhost:3000/", "http://localhost:8000/"]


@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("10.0.0.1", ["10.0.0.1"]),
    ("10.0.0.1, 172.16.0.0/12", ["10.0.0.1", "172.16.0.0/12"]),
    ('["10.0.0.1","::1"]', ["10.0.0.1", "::1"]),
])
def test_trusted_proxies_formats(required, monkeypatch, raw, expected):
    monkeypatch.setenv("TRUSTED_PROXIES", raw)
    assert Settings(_env_file=None).TRUSTED_PROXIES == expected


@pytest.mark.parametrize("raw,expected", [
    ("5,10", [5, 10]),
    ("[5, 10]", [5, 10]),
    ("", [15, 60, 1440]),
])
def test_lockout_tiers_formats(required, monkeypatch, raw, expected):
    monkeypatch.setenv("ADMIN_LOCKOUT_TIERS_MINUTES", raw)
    assert Settings(_env_file=None).ADMIN_LOCKOUT_TIERS_MINUTES == expected


@pytest.mark.parametrize("raw", ["", "http://a.test", "http://a.test,http://b.test", '["http://a.test"]'])
def test_cors_origins_formats(required, monkeypatch, raw):
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", raw)
    Settings(_env_file=None)


def test_unknown_env_file_key_fails_at_boot(required, tmp_path):
    """A misspelled optional secret must not silently fall back to its default."""
    env = tmp_path / ".env"
    env.write_text("PAYSTACK_WEBHOOK_SECRT=typo\n")
    with pytest.raises(Exception, match="PAYSTACK_WEBHOOK_SECRT"):
        Settings(_env_file=env)


def test_compose_only_redis_password_is_accepted(required, tmp_path):
    env = tmp_path / ".env"
    env.write_text("REDIS_PASSWORD=x\n")
    assert Settings(_env_file=env).REDIS_PASSWORD == "x"
