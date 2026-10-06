"""Shared helpers for the admin auth tests: a controllable TOTP clock and a login helper."""

import time
from types import SimpleNamespace

import pyotp
import pytest

from app.tests.factories import DEFAULT_PASSWORD, admin_totp_secret

LOGIN = "/api/v1/admin/auth/login"
VERIFY = "/api/v1/admin/auth/mfa/verify"
REFRESH = "/api/v1/admin/auth/refresh"
LOGOUT = "/api/v1/admin/auth/logout"
ACCEPT = "/api/v1/admin/auth/invite/accept"
ME = "/api/v1/admin/me"
ADMINS = "/api/v1/admin/admins"
AUDIT = "/api/v1/admin/audit"


class FakeClock:
    """The clock the TOTP check reads (``app.services.admin_auth._now_ts``)."""

    def __init__(self) -> None:
        # start in the middle of a 30-second step so +-1 step arithmetic in tests is unambiguous
        self.now = (int(time.time()) // 30) * 30 + 15.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def code(self, secret: str, step_offset: int = 0) -> str:
        return pyotp.TOTP(secret).at(self.now + 30 * step_offset)


@pytest.fixture
def admin_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    import app.services.admin_auth as svc

    monkeypatch.setattr(svc, "_now_ts", clock)
    return clock


def fake_request(ip: str = "203.0.113.9"):
    """Enough of a Request for services that read the client address and user agent."""
    return SimpleNamespace(client=SimpleNamespace(host=ip), headers={"user-agent": "pytest"})


def admin_login(client, clock: FakeClock, admin, password: str = DEFAULT_PASSWORD) -> dict:
    """Full password + TOTP sign-in for an admin who already has MFA enrolled. Returns the verify body."""
    step1 = client.post(LOGIN, json={"email": admin.email, "password": password})
    assert step1.status_code == 200, step1.text
    clock.advance(30)  # a fresh time step every time, so the previous code is never replayed
    r = client.post(VERIFY, json={
        "mfa_token": step1.json()["mfa_token"], "code": clock.code(admin_totp_secret(admin))})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
