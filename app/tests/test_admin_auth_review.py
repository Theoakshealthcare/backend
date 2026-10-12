"""Fixes from the Opus security review of admin auth: client IP, password throttling, MFA lock
escalation, single-use MFA tokens and logout racing a refresh."""

import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.admin_security import create_admin_token
from app.core.client_ip import client_ip
from app.core.config import settings
from app.db.models.admin import AdminRefreshToken, AdminUser
from app.db.models.audit import AdminAuditLog
from app.db.session import SessionLocal
from app.main import app as fastapi_app
from app.services.admin_auth import AdminAuthService
from app.tests.admin_helpers import LOGIN, REFRESH, VERIFY, admin_login, fake_request
from app.tests.factories import DEFAULT_PASSWORD, admin_totp_secret, make_admin

WRONG = "wrong-password-123"


def _from(ip: str) -> TestClient:
    """A client whose direct peer is ``ip`` (what the server sees as request.client.host)."""
    return TestClient(fastapi_app, client=(ip, 50000), raise_server_exceptions=False)


def _login(c, email, password=WRONG, **kw):
    return c.post(LOGIN, json={"email": email, "password": password}, **kw)


@pytest.fixture
def lenient_ip_limit(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 10_000)


def _fresh(db, admin):
    db.expire_all()
    return db.get(AdminUser, admin.id)


# --- 1. client IP ---------------------------------------------------------------------------------


def _req(peer, xff=None):
    headers = {"user-agent": "pytest"}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    req = fake_request(peer)
    req.headers = headers
    return req


def test_no_trusted_proxies_means_xff_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", [])
    assert client_ip(_req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_xff_from_an_untrusted_peer_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert client_ip(_req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_trusted_peer_yields_the_real_client(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert client_ip(_req("10.0.0.5", "198.51.100.7")) == "198.51.100.7"


def test_rightmost_untrusted_hop_wins_over_a_spoofed_left_entry(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8", "172.16.0.1"])
    # the attacker sent "6.6.6.6"; our edge appended the attacker's real address; an inner proxy appended itself
    assert client_ip(_req("10.0.0.5", "6.6.6.6, 198.51.100.7, 172.16.0.1")) == "198.51.100.7"


def test_trusted_peer_without_xff_or_with_junk_falls_back_to_the_peer(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert client_ip(_req("10.0.0.5")) == "10.0.0.5"
    assert client_ip(_req("10.0.0.5", "not-an-ip")) == "10.0.0.5"


def test_trusted_proxies_accepts_a_comma_separated_string(monkeypatch):
    from app.core.config import Settings

    s = Settings(SECRET_KEY="x", DATABASE_URL="postgresql://x:x@h/x", VITALS_ENCRYPTION_KEY="x",
                 TRUSTED_PROXIES="10.0.0.0/8, 192.168.1.1")
    assert s.TRUSTED_PROXIES == ["10.0.0.0/8", "192.168.1.1"]


def test_spoofed_xff_from_an_untrusted_peer_does_not_dodge_the_limit(db, monkeypatch, pinned_ip_window):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", [])
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 3)
    c = _from("203.0.113.50")
    codes = [_login(c, "nobody@example.com", headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code
             for i in range(5)]
    assert codes == [401, 401, 401, 429, 429]


def test_one_noisy_client_behind_a_trusted_proxy_does_not_429_the_others(db, monkeypatch, pinned_ip_window):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 3)
    proxy = _from("10.0.0.5")
    attacker = {"X-Forwarded-For": "198.51.100.1"}
    codes = [_login(proxy, "nobody@example.com", headers=attacker).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    # a different real client through the same proxy is untouched
    other = _login(proxy, "nobody@example.com", headers={"X-Forwarded-For": "198.51.100.2"})
    assert other.status_code == 401


def test_audit_ip_is_the_real_client_behind_a_trusted_proxy(db, monkeypatch, admin_clock):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    admin = make_admin(db)
    c = _from("10.0.0.5")
    h = {"X-Forwarded-For": "198.51.100.7"}
    token = c.post(LOGIN, json={"email": admin.email, "password": DEFAULT_PASSWORD}, headers=h).json()["mfa_token"]
    r = c.post(VERIFY, json={"mfa_token": token, "code": admin_clock.code(admin_totp_secret(admin))}, headers=h)
    assert r.status_code == 200
    ips = {a.ip for a in db.scalars(select(AdminAuditLog))}
    assert ips == {"198.51.100.7"}


def test_refresh_is_not_limited_by_login_traffic(db, client, admin_clock, monkeypatch, pinned_ip_window):
    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 2)
    assert [_login(client, "nobody@example.com").status_code for _ in range(4)][-1] == 429
    r = client.post(REFRESH, json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200


def test_junk_refresh_tokens_neither_count_nor_leave_rows(db, client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 2)
    for i in range(6):
        assert client.post(REFRESH, json={"refresh_token": f"junk-{i}"}).status_code == 401
    assert db.execute(text("SELECT count(*) FROM admin_rate_limits")).scalar_one() == 0


def test_refresh_is_rate_limited_per_token(db, client, admin_clock, monkeypatch, pinned_ip_window):
    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 2)
    tok = body["refresh_token"]
    assert client.post(REFRESH, json={"refresh_token": tok}).status_code == 200
    assert client.post(REFRESH, json={"refresh_token": tok}).status_code == 401
    r = client.post(REFRESH, json={"refresh_token": tok})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


# --- 2. password-stage throttle: backoff, never a hard lock, no enumeration ---------------------


def _sequence(c, email, n):
    out = []
    for _ in range(n):
        r = _login(c, email)
        retry = r.headers.get("Retry-After")
        out.append((r.status_code, r.json(), int(retry) if retry else None))
    return out


def test_password_failures_throttle_with_backoff_and_never_lock(db, lenient_ip_limit):
    admin = make_admin(db)
    c = _from("203.0.113.20")
    seq = _sequence(c, admin.email, settings.ADMIN_PASSWORD_FREE_ATTEMPTS + 3)
    free = settings.ADMIN_PASSWORD_FREE_ATTEMPTS
    assert [s for s, _, _ in seq[:free]] == [401] * free
    assert [s for s, _, _ in seq[free:]] == [429] * 3
    assert seq[free][1] == {"detail": "Too many requests. Try again later."}
    first_wait = seq[free][2]
    assert 1 <= first_wait <= settings.ADMIN_PASSWORD_BACKOFF_SECONDS + 1

    # even the right password is refused while throttled (no oracle)
    assert _login(c, admin.email, DEFAULT_PASSWORD).status_code == 429

    # once the wait is over the next failure doubles it
    db.execute(text("UPDATE admin_rate_limits SET blocked_until = now() at time zone 'utc' - interval '1 second' "
                    "WHERE blocked_until IS NOT NULL"))
    db.commit()
    assert _login(c, admin.email).status_code == 401
    again = _login(c, admin.email)
    assert again.status_code == 429
    assert int(again.headers["Retry-After"]) > first_wait + 20

    after = _fresh(db, admin)
    assert after.failed_logins == 0 and after.locked_until is None  # never a hard lock at this stage


def test_backoff_is_capped(db, lenient_ip_limit, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_FREE_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_BACKOFF_SECONDS", 100)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_BACKOFF_MAX_SECONDS", 250)
    c = _from("203.0.113.21")
    for _ in range(6):
        _login(c, "nobody@example.com")
        db.execute(text("UPDATE admin_rate_limits SET blocked_until = now() at time zone 'utc' - interval '1 second' "
                        "WHERE blocked_until IS NOT NULL"))
        db.commit()
    _login(c, "nobody@example.com")
    r = _login(c, "nobody@example.com")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) <= 251


def test_responses_are_indistinguishable_for_every_kind_of_email(db, lenient_ip_limit):
    cases = {
        "unknown": "nobody@example.com",
        "active": make_admin(db).email,
        "invited": make_admin(db, status="invited").email,
        "disabled": make_admin(db, status="disabled").email,
    }
    n = settings.ADMIN_PASSWORD_FREE_ATTEMPTS + 4
    results = {name: _sequence(_from(f"203.0.113.{100 + i}"), email, n) for i, (name, email) in enumerate(cases.items())}
    baseline = results["unknown"]
    assert baseline[0][0] == 401 and baseline[-1][0] == 429
    for name, seq in results.items():
        assert [(s, b) for s, b, _ in seq] == [(s, b) for s, b, _ in baseline], name
        for (_, _, a), (_, _, b) in zip(seq, baseline):  # Retry-After equal to within clock skew
            assert (a is None) == (b is None) and (a is None or abs(a - b) <= 2), name


def test_correct_password_for_invited_or_disabled_gets_the_same_401(db, lenient_ip_limit):
    for status in ("invited", "disabled"):
        admin = make_admin(db, status=status)
        r = _login(_from("203.0.113.30"), admin.email, DEFAULT_PASSWORD)
        assert r.status_code == 401 and r.json() == {"detail": "Invalid email or password"}


def test_an_attacker_cannot_lock_the_admin_out_from_another_ip(db, lenient_ip_limit):
    admin = make_admin(db)
    attacker, owner = _from("203.0.113.66"), _from("198.51.100.4")
    codes = [_login(attacker, admin.email).status_code for _ in range(10)]
    assert codes.count(429) >= 4
    ok = _login(owner, admin.email, DEFAULT_PASSWORD)
    assert ok.status_code == 200 and ok.json()["mfa_required"] is True
    row = _fresh(db, admin)
    assert row.failed_logins == 0 and row.locked_until is None


def test_throttle_is_per_email_and_ip(db, lenient_ip_limit):
    a, b = make_admin(db), make_admin(db)
    c = _from("203.0.113.70")
    for _ in range(settings.ADMIN_PASSWORD_FREE_ATTEMPTS + 1):
        _login(c, a.email)
    assert _login(c, a.email).status_code == 429
    assert _login(c, b.email).status_code == 401  # a different email from the same IP is a fresh count


def test_a_good_login_clears_the_throttle_for_that_pair(db, lenient_ip_limit):
    admin = make_admin(db)
    c = _from("203.0.113.71")
    for _ in range(settings.ADMIN_PASSWORD_FREE_ATTEMPTS - 1):
        assert _login(c, admin.email).status_code == 401
    assert _login(c, admin.email, DEFAULT_PASSWORD).status_code == 200
    for _ in range(settings.ADMIN_PASSWORD_FREE_ATTEMPTS):
        assert _login(c, admin.email).status_code == 401


def test_unknown_email_still_spends_a_password_hash(db, lenient_ip_limit, monkeypatch):
    import app.services.admin_auth as svc

    calls = []
    real = svc.verify_password
    monkeypatch.setattr(svc, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    _login(_from("203.0.113.72"), "nobody@example.com")
    assert len(calls) == 1


# --- 3. MFA lock escalation -----------------------------------------------------------------------


def _fail_mfa(client, token, n):
    last = None
    for _ in range(n):
        last = client.post(VERIFY, json={"mfa_token": token, "code": "000000"})
    return last


def _expire_lock(db, admin):
    row = _fresh(db, admin)
    row.locked_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()


def test_mfa_lock_duration_escalates_and_each_lock_is_audited(db, client, admin_clock, lenient_ip_limit):
    admin = make_admin(db)
    tiers = settings.ADMIN_LOCKOUT_TIERS_MINUTES
    assert tiers == [15, 60, 1440]
    for i, minutes in enumerate([15, 60, 1440, 1440]):
        token = _login(client, admin.email, DEFAULT_PASSWORD).json()["mfa_token"]
        _fail_mfa(client, token, settings.ADMIN_MAX_FAILED_LOGINS)
        remaining = _fresh(db, admin).locked_until - datetime.utcnow()
        assert timedelta(minutes=minutes - 1) < remaining <= timedelta(minutes=minutes), i
        locked = client.post(VERIFY, json={"mfa_token": token, "code": "000000"})
        assert locked.status_code == 423
        _expire_lock(db, admin)
    locks = list(db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "admin.locked")))
    assert len(locks) == 4 and all(a.after["stage"] == "mfa" for a in locks)
    assert [a.after["minutes"] for a in locks] == [15, 60, 1440, 1440]


def test_successful_mfa_resets_the_escalation(db, client, admin_clock, lenient_ip_limit):
    admin = make_admin(db)
    token = _login(client, admin.email, DEFAULT_PASSWORD).json()["mfa_token"]
    _fail_mfa(client, token, settings.ADMIN_MAX_FAILED_LOGINS)
    assert _fresh(db, admin).mfa_lock_count == 1
    _expire_lock(db, admin)
    admin_login(client, admin_clock, admin)
    assert _fresh(db, admin).mfa_lock_count == 0
    token = _login(client, admin.email, DEFAULT_PASSWORD).json()["mfa_token"]
    _fail_mfa(client, token, settings.ADMIN_MAX_FAILED_LOGINS)
    assert _fresh(db, admin).locked_until - datetime.utcnow() <= timedelta(minutes=15)


def test_mfa_lock_applies_only_after_the_password_is_proven(db, client, lenient_ip_limit):
    admin = make_admin(db)
    token = _login(client, admin.email, DEFAULT_PASSWORD).json()["mfa_token"]
    _fail_mfa(client, token, settings.ADMIN_MAX_FAILED_LOGINS)
    locked = _login(client, admin.email, DEFAULT_PASSWORD)
    assert locked.status_code == 423 and int(locked.headers["Retry-After"]) > 0
    # a wrong password cannot tell the account is locked (no enumeration of locked accounts)
    wrong = _login(_from("203.0.113.88"), admin.email)
    assert wrong.status_code == 401 and wrong.json() == {"detail": "Invalid email or password"}


# --- 4. mfa_token is single use -------------------------------------------------------------------


def test_an_mfa_token_cannot_be_used_twice(db, client, admin_clock):
    admin = make_admin(db)
    secret = admin_totp_secret(admin)
    token = _login(client, admin.email, DEFAULT_PASSWORD).json()["mfa_token"]
    first = client.post(VERIFY, json={"mfa_token": token, "code": admin_clock.code(secret)})
    assert first.status_code == 200
    admin_clock.advance(30)  # a fresh, valid code: only the token is spent
    again = client.post(VERIFY, json={"mfa_token": token, "code": admin_clock.code(secret)})
    assert again.status_code == 401 and again.json() == {"detail": "Invalid or expired MFA token"}
    assert _fresh(db, admin).failed_logins == 0  # reuse is not a code guess


def test_wrong_codes_do_not_spend_the_token(db, client, admin_clock):
    admin = make_admin(db)
    token = _login(client, admin.email, DEFAULT_PASSWORD).json()["mfa_token"]
    assert client.post(VERIFY, json={"mfa_token": token, "code": "000000"}).status_code == 401
    ok = client.post(VERIFY, json={"mfa_token": token, "code": admin_clock.code(admin_totp_secret(admin))})
    assert ok.status_code == 200


def test_two_simultaneous_uses_of_one_token_log_in_once(db, admin_clock):
    admin = make_admin(db)
    secret = admin_totp_secret(admin)
    token = create_admin_token(admin.id, "mfa", timedelta(minutes=5))
    codes = [admin_clock.code(secret)] * 2  # the token check runs before the code check, so it is what refuses the loser
    barrier, results = threading.Barrier(2), []

    def attempt(code):
        session = SessionLocal()
        try:
            barrier.wait()
            AdminAuthService(session).verify_mfa(token, code, fake_request())
            results.append("ok")
        except Exception as exc:  # noqa: BLE001 - the test records which error type each thread got
            results.append(type(exc).__name__)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(c,)) for c in codes]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["InvalidMfaToken", "ok"]


# --- 5. logout waits for an in-flight refresh ---------------------------------------------------


def test_logout_during_a_refresh_revokes_the_token_the_refresh_issues(db, client, admin_clock, monkeypatch):
    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    in_flight, release = threading.Event(), threading.Event()
    real = AdminAuthService._new_refresh_row

    def slow(self, *a, **kw):  # runs while the refresh holds the lock on the old token's row
        in_flight.set()
        assert release.wait(10)
        return real(self, *a, **kw)

    monkeypatch.setattr(AdminAuthService, "_new_refresh_row", slow)
    out = {}

    def do_refresh():
        s = SessionLocal()
        try:
            out["refresh"] = AdminAuthService(s).refresh(body["refresh_token"], fake_request("203.0.113.5"))
        finally:
            s.close()

    def do_logout():
        s = SessionLocal()
        try:
            AdminAuthService(s).logout(body["refresh_token"])
            out["logout_done"] = time.monotonic()
        finally:
            s.close()

    t1 = threading.Thread(target=do_refresh)
    t1.start()
    assert in_flight.wait(10)
    t2 = threading.Thread(target=do_logout)
    t2.start()
    time.sleep(0.6)
    assert t2.is_alive(), "logout must wait for the refresh in flight"
    release.set()
    t1.join(10)
    t2.join(10)
    assert "refresh" in out and "logout_done" in out

    db.expire_all()
    live = list(db.scalars(select(AdminRefreshToken).where(AdminRefreshToken.revoked_at.is_(None))))
    assert live == []
