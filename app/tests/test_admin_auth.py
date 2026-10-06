"""Admin sign-in: password, TOTP enrolment and replay, token audiences, refresh rotation, lockout."""

import threading
import uuid
from datetime import datetime, timedelta

import pytest
from jose import jwt as jose_jwt
from sqlalchemy import select

from app.core.admin_security import create_admin_token
from app.core.config import settings
from app.db.models.admin import AdminRefreshToken, AdminUser
from app.db.models.audit import AdminAuditLog
from app.db.session import SessionLocal
from app.services.admin_auth import AdminAuthService, InvalidMfaCode
from app.tests.admin_helpers import (
    LOGIN,
    LOGOUT,
    ME,
    REFRESH,
    VERIFY,
    admin_login,
    bearer,
    fake_request,
)
from app.tests.factories import (
    DEFAULT_PASSWORD,
    admin_totp_secret,
    auth_headers,
    make_admin,
    make_caregiver,
    make_patient,
    refresh_headers,
)


def _fresh(db, admin):
    db.expire_all()
    return db.get(AdminUser, admin.id)


def _login(client, admin, password=DEFAULT_PASSWORD):
    return client.post(LOGIN, json={"email": admin.email, "password": password})


# --- password step ---------------------------------------------------------------------------


def test_login_enrolled_admin_asks_for_a_code(db, client):
    admin = make_admin(db)
    r = _login(client, admin)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"mfa_required", "mfa_token"} and body["mfa_required"] is True


def test_login_email_is_case_insensitive(db, client):
    make_admin(db, email="mixed.case@example.com")
    r = client.post(LOGIN, json={"email": "Mixed.Case@Example.com", "password": DEFAULT_PASSWORD})
    assert r.status_code == 200


@pytest.mark.parametrize("case", ["wrong_password", "unknown_email", "disabled", "invited"])
def test_bad_credentials_all_look_the_same(db, client, case):
    admin = make_admin(db, status={"disabled": "disabled", "invited": "invited"}.get(case, "active"))
    email = "nobody@example.com" if case == "unknown_email" else admin.email
    password = "wrong-password-123" if case == "wrong_password" else DEFAULT_PASSWORD
    r = client.post(LOGIN, json={"email": email, "password": password})
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid email or password"}


def test_login_does_not_accept_an_app_user_account(db, client):
    patient = make_patient(db)
    r = client.post(LOGIN, json={"email": patient.user.email, "password": DEFAULT_PASSWORD})
    assert r.status_code == 401


# --- MFA enrolment and verify -------------------------------------------------------------------


def test_first_login_requires_enrolment_and_completes_on_first_verify(db, client, admin_clock):
    admin = make_admin(db, mfa=False)
    r = _login(client, admin)
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_enrollment_required"] is True and "mfa_required" not in body
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert "secret=" + body["secret"] in body["otpauth_uri"]
    assert admin.email.replace("@", "%40") in body["otpauth_uri"]
    assert _fresh(db, admin).mfa_enabled is False

    # logging in again before enrolling hands out the same secret
    assert _login(client, admin).json()["secret"] == body["secret"]

    # the secret is encrypted at rest
    assert body["secret"] not in _fresh(db, admin).mfa_secret_enc

    bad = client.post(VERIFY, json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert bad.status_code == 401 and bad.json() == {"detail": "Invalid verification code"}
    assert _fresh(db, admin).mfa_enabled is False

    ok = client.post(VERIFY, json={"mfa_token": body["mfa_token"], "code": admin_clock.code(body["secret"])})
    assert ok.status_code == 200, ok.text
    assert _fresh(db, admin).mfa_enabled is True
    # next login is a normal MFA login
    assert _login(client, admin).json().get("mfa_required") is True


def test_verify_returns_tokens_and_the_admin(db, client, admin_clock):
    admin = make_admin(db, ["verification"], full_name="Vera Verifier")
    body = admin_login(client, admin_clock, admin)
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in", "admin"}
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    assert body["admin"]["id"] == str(admin.id)
    assert body["admin"]["roles"] == ["verification"]
    assert "caregivers.approve" in body["admin"]["permissions"]
    assert body["admin"]["permissions"] == sorted(body["admin"]["permissions"])
    assert "admins.manage" not in body["admin"]["permissions"]

    me = client.get(ME, headers=bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json() == body["admin"]
    assert _fresh(db, admin).last_login_at is not None


def test_access_token_claims(db, client, admin_clock):
    import jwt

    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["aud"] == "admin" and claims["type"] == "access" and claims["sub"] == str(admin.id)
    assert claims["exp"] - claims["iat"] == 15 * 60


def test_wrong_code_is_401_and_does_not_log_in(db, client, admin_clock):
    admin = make_admin(db)
    token = _login(client, admin).json()["mfa_token"]
    r = client.post(VERIFY, json={"mfa_token": token, "code": "123456"})
    assert r.status_code == 401
    assert _fresh(db, admin).last_login_at is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦"])
def test_malformed_codes_are_rejected(db, client, admin_clock, code):
    admin = make_admin(db)
    token = _login(client, admin).json()["mfa_token"]
    r = client.post(VERIFY, json={"mfa_token": token, "code": code})
    assert r.status_code in (401, 422)


def test_a_code_cannot_be_replayed_within_its_time_step(db, client, admin_clock):
    admin = make_admin(db)
    secret = admin_totp_secret(admin)
    code = admin_clock.code(secret)

    first = client.post(VERIFY, json={"mfa_token": _login(client, admin).json()["mfa_token"], "code": code})
    assert first.status_code == 200

    # same code, same 30-second step, brand-new password step: refused
    again = client.post(VERIFY, json={"mfa_token": _login(client, admin).json()["mfa_token"], "code": code})
    assert again.status_code == 401 and again.json() == {"detail": "Invalid verification code"}

    # an earlier step is refused too, even though it is inside the drift window
    earlier = client.post(VERIFY, json={"mfa_token": _login(client, admin).json()["mfa_token"],
                                        "code": admin_clock.code(secret, -1)})
    assert earlier.status_code == 401

    # the next step works
    admin_clock.advance(30)
    nxt = client.post(VERIFY, json={"mfa_token": _login(client, admin).json()["mfa_token"],
                                    "code": admin_clock.code(secret)})
    assert nxt.status_code == 200


def test_clock_drift_of_one_step_is_accepted_two_is_not(db, client, admin_clock):
    admin = make_admin(db)
    secret = admin_totp_secret(admin)
    tok = lambda: _login(client, admin).json()["mfa_token"]  # noqa: E731

    assert client.post(VERIFY, json={"mfa_token": tok(), "code": admin_clock.code(secret, 2)}).status_code == 401
    assert client.post(VERIFY, json={"mfa_token": tok(), "code": admin_clock.code(secret, -2)}).status_code == 401
    assert client.post(VERIFY, json={"mfa_token": tok(), "code": admin_clock.code(secret, 1)}).status_code == 200


def test_concurrent_submissions_of_one_code_log_in_once(db, admin_clock):
    # Without the row lock both threads would read mfa_last_step before either wrote it.
    admin = make_admin(db)
    secret = admin_totp_secret(admin)
    code = admin_clock.code(secret)
    mfa_token = create_admin_token(admin.id, "mfa", timedelta(minutes=5))
    barrier = threading.Barrier(2)
    results = []

    def attempt():
        session = SessionLocal()
        try:
            barrier.wait()
            AdminAuthService(session).verify_mfa(mfa_token, code, fake_request())
            results.append("ok")
        except InvalidMfaCode:
            results.append("refused")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok", "refused"]


@pytest.mark.parametrize("kind", ["garbage", "expired", "wrong_type", "other_admin_key"])
def test_bad_mfa_tokens_are_401(db, client, admin_clock, kind):
    admin = make_admin(db)
    code = admin_clock.code(admin_totp_secret(admin))
    token = {
        "garbage": "not-a-token",
        "expired": create_admin_token(admin.id, "mfa", timedelta(seconds=-5)),
        "wrong_type": create_admin_token(admin.id, "access", timedelta(minutes=5), sid=str(uuid.uuid4())),
        "other_admin_key": jose_jwt.encode(
            {"sub": str(admin.id), "aud": "admin", "type": "mfa",
             "exp": datetime.utcnow() + timedelta(minutes=5)}, settings.SECRET_KEY, algorithm="HS256"),
    }[kind]
    r = client.post(VERIFY, json={"mfa_token": token, "code": code})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid or expired MFA token"}


def test_disabled_admin_cannot_finish_the_mfa_step(db, client, admin_clock):
    admin = make_admin(db)
    token = _login(client, admin).json()["mfa_token"]
    admin.status = "disabled"
    db.commit()
    r = client.post(VERIFY, json={"mfa_token": token, "code": admin_clock.code(admin_totp_secret(admin))})
    assert r.status_code == 401


def test_sign_in_is_audited(db, client, admin_clock):
    admin = make_admin(db, mfa=False)
    body = _login(client, admin).json()
    client.post(VERIFY, json={"mfa_token": body["mfa_token"], "code": admin_clock.code(body["secret"])})
    actions = [a.action for a in db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at))]
    assert actions == ["admin.mfa_enrolled", "admin.login"]
    row = db.scalars(select(AdminAuditLog)).first()
    assert row.admin_user_id == admin.id and row.ip


# --- token audience: admin and app tokens never cross ---------------------------------------


def test_app_tokens_are_refused_on_admin_routes(db, client):
    patient, caregiver = make_patient(db), make_caregiver(db)
    for user in (patient.user, caregiver.user):
        assert client.get(ME, headers=auth_headers(user)).status_code == 401
        assert client.get("/api/v1/admin/caregivers", headers=auth_headers(user)).status_code == 401
        assert client.get("/api/v1/admin/admins", headers=auth_headers(user)).status_code == 401


def test_admin_tokens_are_refused_on_app_routes(db, client):
    admin = make_admin(db)
    h = auth_headers(admin)
    assert client.get(ME, headers=h).status_code == 200  # the token itself is good
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401
    assert client.get("/api/v1/caregiver/home", headers=h).status_code == 401
    assert client.get("/api/v1/patient/wallet", headers=h).status_code == 401
    assert client.post("/api/v1/devices/push-token", headers=h,
                       json={"token": "ExponentPushToken[x]", "platform": "ios"}).status_code == 401


def test_admin_token_cannot_open_an_app_websocket(db, client):
    from starlette.websockets import WebSocketDisconnect

    admin = make_admin(db)
    token = auth_headers(admin)["Authorization"].split()[1]
    with client.websocket_connect(f"/api/v1/booking/ws/caregiver/location?token={token}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4401


def test_an_app_token_for_an_admins_id_is_still_refused(db, client):
    # forge an app-style token (no audience) whose subject is an admin's id
    from app.core.security import create_access_token

    admin = make_admin(db)
    h = {"Authorization": f"Bearer {create_access_token(admin.id)}"}
    assert client.get(ME, headers=h).status_code == 401


@pytest.mark.parametrize("kind", ["refresh", "mfa", "expired", "signed_with_app_key", "no_audience_claim", "no_session"])
def test_bad_admin_bearer_tokens_are_401(db, client, kind):
    admin = make_admin(db)
    sid = str(uuid.uuid4())
    token = {
        "refresh": create_admin_token(admin.id, "refresh", timedelta(minutes=5), sid=sid),
        "mfa": create_admin_token(admin.id, "mfa", timedelta(minutes=5)),
        "expired": create_admin_token(admin.id, "access", timedelta(seconds=-5), sid=sid),
        "signed_with_app_key": jose_jwt.encode(
            {"sub": str(admin.id), "aud": "admin", "type": "access", "sid": sid,
             "exp": datetime.utcnow() + timedelta(minutes=5)}, settings.SECRET_KEY, algorithm="HS256"),
        "no_audience_claim": jose_jwt.encode(
            {"sub": str(admin.id), "type": "access", "sid": sid,
             "exp": datetime.utcnow() + timedelta(minutes=5)}, settings.SECRET_KEY, algorithm="HS256"),
        "no_session": create_admin_token(admin.id, "access", timedelta(minutes=5), sid=sid),  # no refresh row
    }[kind]
    assert client.get(ME, headers=bearer(token)).status_code == 401


def test_refresh_token_as_bearer_is_401(db, client):
    assert client.get(ME, headers=refresh_headers(make_admin(db))).status_code == 401


def test_no_token_is_401(client):
    r = client.get(ME)
    assert r.status_code == 401 and r.json() == {"detail": "Not authenticated"}


def test_disabled_admin_token_stops_working_at_once(db, client):
    admin = make_admin(db)
    h = auth_headers(admin)
    assert client.get(ME, headers=h).status_code == 200
    admin.status = "disabled"
    db.commit()
    assert client.get(ME, headers=h).status_code == 401


def test_permissions_are_read_live_not_from_the_token(db, client):
    from app.db.models.admin import AdminRole

    admin = make_admin(db, ["verification"])
    h = auth_headers(admin)
    assert client.get("/api/v1/admin/caregivers", headers=h).status_code == 200
    admin.roles = [db.scalar(select(AdminRole).where(AdminRole.key == "read_only_analyst"))]
    db.commit()
    assert client.get("/api/v1/admin/caregivers", headers=h).status_code == 403


# --- refresh rotation, reuse detection, logout ---------------------------------------------------


def test_refresh_rotates_the_pair_and_retires_the_old_token(db, client, admin_clock):
    admin = make_admin(db)
    first = admin_login(client, admin_clock, admin)

    r = client.post(REFRESH, json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert set(second) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert second["refresh_token"] != first["refresh_token"]
    assert second["expires_in"] == 900
    assert client.get(ME, headers=bearer(second["access_token"])).status_code == 200
    # the previous access token lives out its own 15 minutes (same session)
    assert client.get(ME, headers=bearer(first["access_token"])).status_code == 200

    # the retired token no longer works
    assert client.post(REFRESH, json={"refresh_token": first["refresh_token"]}).status_code == 401
    # refresh tokens are stored hashed
    assert not db.scalars(select(AdminRefreshToken).where(AdminRefreshToken.token_hash == second["refresh_token"])).first()


def test_reusing_a_rotated_refresh_token_revokes_every_session(db, client, admin_clock):
    admin = make_admin(db)
    laptop = admin_login(client, admin_clock, admin)
    phone = admin_login(client, admin_clock, admin)
    rotated = client.post(REFRESH, json={"refresh_token": laptop["refresh_token"]}).json()

    # an attacker (or a confused client) replays the old token
    r = client.post(REFRESH, json={"refresh_token": laptop["refresh_token"]})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid or expired refresh token"}

    # every session of this admin is dead: the winner's refresh token and access token, and the other device
    assert client.post(REFRESH, json={"refresh_token": rotated["refresh_token"]}).status_code == 401
    assert client.get(ME, headers=bearer(rotated["access_token"])).status_code == 401
    assert client.post(REFRESH, json={"refresh_token": phone["refresh_token"]}).status_code == 401
    assert client.get(ME, headers=bearer(phone["access_token"])).status_code == 401

    assert "admin.refresh_token_reuse" in [a.action for a in db.scalars(select(AdminAuditLog))]
    # and the admin can sign in again afterwards
    assert admin_login(client, admin_clock, admin)["access_token"]


def test_reuse_only_affects_that_admin(db, client, admin_clock):
    mine, theirs = make_admin(db), make_admin(db)
    a = admin_login(client, admin_clock, mine)
    b = admin_login(client, admin_clock, theirs)
    client.post(REFRESH, json={"refresh_token": a["refresh_token"]})
    client.post(REFRESH, json={"refresh_token": a["refresh_token"]})  # reuse
    assert client.get(ME, headers=bearer(b["access_token"])).status_code == 200


def test_unknown_refresh_token_is_401(client):
    assert client.post(REFRESH, json={"refresh_token": "nope"}).status_code == 401


def test_logout_ends_the_session_immediately(db, client, admin_clock):
    admin = make_admin(db)
    mine = admin_login(client, admin_clock, admin)
    other = admin_login(client, admin_clock, admin)

    r = client.post(LOGOUT, json={"refresh_token": mine["refresh_token"]})
    assert r.status_code == 204 and r.content == b""
    assert client.get(ME, headers=bearer(mine["access_token"])).status_code == 401
    assert client.post(REFRESH, json={"refresh_token": mine["refresh_token"]}).status_code == 401
    # logging out is not treated as theft: the other device is untouched
    assert client.get(ME, headers=bearer(other["access_token"])).status_code == 200
    assert client.post(REFRESH, json={"refresh_token": other["refresh_token"]}).status_code == 200


def test_logout_is_idempotent_and_does_not_reveal_unknown_tokens(client):
    assert client.post(LOGOUT, json={"refresh_token": "never-issued"}).status_code == 204


def test_idle_session_cannot_refresh(db, client, admin_clock):
    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    row = db.scalars(select(AdminRefreshToken)).one()
    assert row.expires_at - row.issued_at == timedelta(minutes=settings.ADMIN_IDLE_TIMEOUT_MINUTES)
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)  # idle for more than 30 minutes
    db.commit()
    assert client.post(REFRESH, json={"refresh_token": body["refresh_token"]}).status_code == 401
    assert client.get(ME, headers=bearer(body["access_token"])).status_code == 401


def test_session_has_an_absolute_lifetime(db, client, admin_clock):
    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    row = db.scalars(select(AdminRefreshToken)).one()
    row.session_started_at = datetime.utcnow() - timedelta(hours=settings.ADMIN_SESSION_MAX_HOURS, minutes=1)
    db.commit()
    assert client.post(REFRESH, json={"refresh_token": body["refresh_token"]}).status_code == 401


def test_refresh_extends_the_idle_window(db, client, admin_clock):
    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    old = db.scalars(select(AdminRefreshToken)).one()
    old.issued_at = old.issued_at - timedelta(minutes=20)
    old.expires_at = datetime.utcnow() + timedelta(minutes=10)
    db.commit()
    assert client.post(REFRESH, json={"refresh_token": body["refresh_token"]}).status_code == 200
    db.expire_all()
    newest = db.scalars(select(AdminRefreshToken).order_by(AdminRefreshToken.issued_at.desc())).first()
    assert newest.expires_at > datetime.utcnow() + timedelta(minutes=25)


def test_disabled_admin_cannot_refresh(db, client, admin_clock):
    admin = make_admin(db)
    body = admin_login(client, admin_clock, admin)
    admin.status = "disabled"
    db.commit()
    assert client.post(REFRESH, json={"refresh_token": body["refresh_token"]}).status_code == 401


# --- lockout and rate limits -------------------------------------------------------------------


def test_ten_failures_lock_the_account(db, client):
    admin = make_admin(db)
    for i in range(settings.ADMIN_MAX_FAILED_LOGINS):
        r = _login(client, admin, "wrong-password-123")
        assert r.status_code == 401, i

    locked = _login(client, admin, "wrong-password-123")
    assert locked.status_code == 423
    assert locked.json() == {"detail": "Account temporarily locked. Try again later."}
    retry = int(locked.headers["Retry-After"])
    assert 0 < retry <= settings.ADMIN_LOCKOUT_MINUTES * 60

    # the right password does not get in while locked
    assert _login(client, admin).status_code == 423
    assert "admin.locked" in [a.action for a in db.scalars(select(AdminAuditLog))]


def test_lock_expires_and_a_good_login_clears_the_counter(db, client, admin_clock):
    admin = make_admin(db)
    for _ in range(settings.ADMIN_MAX_FAILED_LOGINS + 1):
        _login(client, admin, "wrong-password-123")
    row = _fresh(db, admin)
    row.locked_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    for _ in range(3):
        _login(client, admin, "wrong-password-123")
    assert _fresh(db, admin).failed_logins == 3
    admin_login(client, admin_clock, admin)
    after = _fresh(db, admin)
    assert after.failed_logins == 0 and after.locked_until is None


def test_failures_in_the_mfa_step_count_towards_the_lock(db, client, admin_clock):
    admin = make_admin(db)
    token = _login(client, admin).json()["mfa_token"]
    for i in range(settings.ADMIN_MAX_FAILED_LOGINS):
        assert client.post(VERIFY, json={"mfa_token": token, "code": "000000"}).status_code == 401, i
    r = client.post(VERIFY, json={"mfa_token": token, "code": admin_clock.code(admin_totp_secret(admin))})
    assert r.status_code == 423 and int(r.headers["Retry-After"]) > 0


def test_a_correct_password_does_not_reset_mfa_guessing(db, client):
    # otherwise an attacker holding the password could alternate password and code guesses forever
    admin = make_admin(db)
    for _ in range(settings.ADMIN_MAX_FAILED_LOGINS - 1):
        token = _login(client, admin).json()["mfa_token"]
        client.post(VERIFY, json={"mfa_token": token, "code": "000000"})
    assert _fresh(db, admin).failed_logins == settings.ADMIN_MAX_FAILED_LOGINS - 1


def test_locking_one_account_does_not_lock_another(db, client):
    a, b = make_admin(db), make_admin(db)
    for _ in range(settings.ADMIN_MAX_FAILED_LOGINS + 1):
        _login(client, a, "wrong-password-123")
    assert _login(client, a).status_code == 423
    assert _login(client, b).status_code == 200


def test_per_ip_rate_limit(db, client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 5)
    admin = make_admin(db)
    codes = [_login(client, admin, "wrong-password-123").status_code for _ in range(7)]
    assert codes[:5] == [401] * 5
    assert codes[5:] == [429, 429]
    r = _login(client, admin)
    assert r.status_code == 429
    assert r.json() == {"detail": "Too many requests. Try again later."}
    assert 1 <= int(r.headers["Retry-After"]) <= settings.ADMIN_AUTH_IP_WINDOW_SECONDS + 1
    # throttled calls never reached the password check, so they did not add failures
    assert _fresh(db, admin).failed_logins == 5


def test_rate_limit_also_covers_verify_and_refresh(db, client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 2)
    assert client.post(REFRESH, json={"refresh_token": "x"}).status_code == 401
    assert client.post(REFRESH, json={"refresh_token": "x"}).status_code == 401
    assert client.post(REFRESH, json={"refresh_token": "x"}).status_code == 429
    assert client.post(VERIFY, json={"mfa_token": "x", "code": "000000"}).status_code == 429


def test_rate_limit_window_rolls_over(db, client, monkeypatch):
    from sqlalchemy import text

    monkeypatch.setattr(settings, "ADMIN_AUTH_IP_LIMIT", 1)
    client.post(REFRESH, json={"refresh_token": "x"})
    assert client.post(REFRESH, json={"refresh_token": "x"}).status_code == 429
    db.execute(text("UPDATE admin_rate_limits SET window_start = window_start - interval '1 hour'"))
    db.commit()
    assert client.post(REFRESH, json={"refresh_token": "x"}).status_code == 401
