"""Test harness: a throwaway Postgres, factories, auth helpers and a network guard.

How it works
- Before the app is imported, DATABASE_URL is pointed at a disposable Postgres
  (a testcontainers container, or TEST_DATABASE_URL if set, e.g. a CI service).
  The app's engine is created at import time, so every get_db() in the codebase
  uses the test database without dependency overrides.
- The schema is built by running the Alembic migrations (``alembic upgrade head``) once
  per session, so tests run against exactly what production gets; every test starts
  from empty tables (TRUNCATE ... CASCADE).
- The developer's real .env is never loaded (OAKLY_ENV_FILE=""), and outbound
  network (SMTP, HTTP) is blocked, so a test can never send email, charge
  Paystack or call Gemini. Tests that need a third party mock it explicitly.
"""

import os
import smtplib

import pytest
from cryptography.fernet import Fernet

# --- environment: must be set before anything from `app` is imported ---------
os.environ["OAKLY_ENV_FILE"] = ""
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("VITALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_placeholder")
os.environ.setdefault("PAYSTACK_WEBHOOK_SECRET", "whsec_test_placeholder")
os.environ.setdefault("ENVIRONMENT", "test")

_container = None
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    from testcontainers.postgres import PostgresContainer

    _container = PostgresContainer("postgres:16-alpine", driver="psycopg2")
    _container.start()
    os.environ["DATABASE_URL"] = _container.get_connection_url()

# --- app imports (after env) -------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.db.models  # noqa: E402,F401  (registers every model on Base.metadata)
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.tests.guards import NetworkBlocked  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    if _container is not None:
        _container.stop()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from alembic import command
    from alembic.config import Config

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config("alembic.ini")
    cfg.attributes["configure_logger"] = False  # keep pytest's logging setup
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_schema):
    yield
    # admin_roles / admin_role_permissions are reference data seeded by the migration; keep them
    tables = ", ".join(
        f'"{t.name}"' for t in Base.metadata.sorted_tables
        if t.name not in ("admin_roles", "admin_role_permissions")
    )
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Fail loudly on any real SMTP or HTTP call. Mock the service instead."""

    def _refuse(*args, **kwargs):
        raise NetworkBlocked("Outbound network is disabled in tests; mock the service call.")

    monkeypatch.setattr(smtplib, "SMTP", _refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _refuse)
    import requests

    monkeypatch.setattr(requests.sessions.Session, "request", _refuse)
    import httpx

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse)


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture emails instead of sending them. Returns a list of (to, kind, payload)."""
    captured = []
    import app.api.v1.endpoints.auth as auth_endpoints

    monkeypatch.setattr(
        auth_endpoints, "send_verification_email",
        lambda email_to, code: captured.append((email_to, "verification", code)),
    )
    monkeypatch.setattr(
        auth_endpoints, "send_password_reset_email",
        lambda email_to, token: captured.append((email_to, "password_reset", token)),
    )
    return captured


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    # raise_server_exceptions=False: a 500 is returned as a response, so tests can
    # assert on status codes the way a real client would see them.
    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        yield c


# factories live in a plain module so tests can import them directly
from app.tests.factories import *  # noqa: E402,F401,F403
from app.tests.fakes import expo  # noqa: E402,F401  (fixture)
from app.tests.admin_helpers import admin_clock  # noqa: E402,F401  (fixture)
