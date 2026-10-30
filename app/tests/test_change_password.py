"""POST /auth/change-password: the caller proves the current password, picks a new one, every OTHER session ends,
and the caller gets a fresh token pair so they stay signed in."""

import logging
from datetime import datetime, timedelta

import jwt
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token, verify_password
from app.db.models.admin import AdminRateLimit
from app.tests.factories import DEFAULT_PASSWORD, auth_headers, make_caregiver, make_patient

CHANGE = "/api/v1/auth/change-password"
LOGIN = "/api/v1/auth/login/mobile"
REFRESH = "/api/v1/auth/refresh"
ME = "/api/v1/auth/me"
NEW_PASSWORD = "Brand-New-Pass-42"


def body(current=DEFAULT_PASSWORD, new=NEW_PASSWORD):
    return {"current_password": current, "new_password": new}


def sign_in(client, user, password=DEFAULT_PASSWORD):
    r = client.post(LOGIN, json={"email_or_phone": user.email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def error(response):
    return response.json()["detail"]


def test_requires_a_token(client):
    assert client.post(CHANGE, json=body()).status_code == 401


def test_a_refresh_token_cannot_change_the_password(db, client):
    user = make_caregiver(db).user
    headers = {"Authorization": f"Bearer {create_refresh_token(user.id)}"}
    assert client.post(CHANGE, json=body(), headers=headers).status_code == 401


def test_missing_fields_are_422(db, client):
    user = make_caregiver(db).user
    r = client.post(CHANGE, json={"current_password": DEFAULT_PASSWORD}, headers=auth_headers(user))
    assert r.status_code == 422


def test_a_422_does_not_echo_the_passwords(db, client):
    user = make_caregiver(db).user
    for partial in ({"current_password": DEFAULT_PASSWORD}, {"new_password": NEW_PASSWORD}):
        r = client.post(CHANGE, json=partial, headers=auth_headers(user))
        assert r.status_code == 422
        assert DEFAULT_PASSWORD not in r.text and NEW_PASSWORD not in r.text


def test_wrong_current_password(db, client):
    user = make_caregiver(db).user
    r = client.post(CHANGE, json=body(current="not-the-password"), headers=auth_headers(user))
    assert r.status_code == 400
    assert error(r) == {"code": "wrong_current_password", "message": "Your current password is incorrect."}
    db.refresh(user)
    assert verify_password(DEFAULT_PASSWORD, user.hashed_password)  # unchanged
    assert user.token_version == 0


@pytest.mark.parametrize("new", ["short1", "a" * 5 + "bcd", "x" * 73, "é" * 37, "        "])
def test_weak_new_password(db, client, new):
    # too short, too few distinct characters, 73 bytes, 74 bytes (37 two-byte characters), blank
    user = make_caregiver(db).user
    r = client.post(CHANGE, json=body(new=new), headers=auth_headers(user))
    assert r.status_code == 400, r.text
    assert error(r)["code"] == "weak_password"
    db.refresh(user)
    assert verify_password(DEFAULT_PASSWORD, user.hashed_password)


def test_same_password_as_the_current_one(db, client):
    user = make_caregiver(db).user
    r = client.post(CHANGE, json=body(new=DEFAULT_PASSWORD), headers=auth_headers(user))
    assert r.status_code == 400
    assert error(r) == {"code": "same_password", "message": "Choose a password you are not using now."}


def test_wrong_current_is_reported_before_a_weak_new_password(db, client):
    user = make_caregiver(db).user
    r = client.post(CHANGE, json=body(current="nope", new="x"), headers=auth_headers(user))
    assert error(r)["code"] == "wrong_current_password"


def test_success_then_the_old_password_stops_working(db, client):
    user = make_caregiver(db).user
    r = client.post(CHANGE, json=body(), headers=auth_headers(user))
    assert r.status_code == 200, r.text
    assert r.json()["detail"] == "Password changed."
    old = client.post(LOGIN, json={"email_or_phone": user.email, "password": DEFAULT_PASSWORD})
    assert old.status_code == 401
    assert sign_in(client, user, NEW_PASSWORD)["token_type"] == "bearer"


def test_works_for_a_patient_too(db, client):
    user = make_patient(db).user
    assert client.post(CHANGE, json=body(), headers=auth_headers(user)).status_code == 200


def test_the_caller_stays_signed_in_on_the_returned_tokens(db, client):
    user = make_caregiver(db).user
    session = sign_in(client, user)
    r = client.post(CHANGE, json=body(), headers=bearer(session))
    fresh = r.json()
    assert client.get(ME, headers=bearer(fresh)).status_code == 200
    refreshed = client.post(REFRESH, json={"refresh_token": fresh["refresh_token"]})
    assert refreshed.status_code == 200
    assert client.get(ME, headers=bearer(refreshed.json())).status_code == 200


def test_other_sessions_are_revoked_access_and_refresh(db, client):
    user = make_caregiver(db).user
    this_phone = sign_in(client, user)
    # a token has no unique id, so a login in the same second would be the very same token: sign the other one differently
    other_phone = {"access_token": create_access_token(user.id, timedelta(minutes=7)),
                   "refresh_token": create_refresh_token(user.id, timedelta(days=3))}
    assert other_phone["access_token"] != this_phone["access_token"]
    assert other_phone["refresh_token"] != this_phone["refresh_token"]
    assert client.post(CHANGE, json=body(), headers=bearer(this_phone)).status_code == 200
    for tokens in (this_phone, other_phone):  # the tokens issued before the change are all dead, the caller's too
        assert client.get(ME, headers=bearer(tokens)).status_code == 401
        r = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid refresh token"


def test_a_pending_reset_link_is_cancelled(db, client):
    user = make_caregiver(db).user
    user.password_reset_token = "pending-reset-token"
    user.password_reset_expires_at = datetime.utcnow() + timedelta(hours=1)
    db.commit()
    client.post(CHANGE, json=body(), headers=auth_headers(user))
    db.refresh(user)
    assert user.password_reset_token is None and user.password_reset_expires_at is None


def test_a_token_issued_before_this_feature_has_no_version_and_still_works(db, client):
    user = make_caregiver(db).user
    legacy = jwt.encode(
        {"sub": str(user.id), "type": "access", "exp": datetime.utcnow() + timedelta(minutes=5)},
        settings.SECRET_KEY, algorithm="HS256")
    assert client.get(ME, headers={"Authorization": f"Bearer {legacy}"}).status_code == 200
    client.post(CHANGE, json=body(), headers=auth_headers(user))
    assert client.get(ME, headers={"Authorization": f"Bearer {legacy}"}).status_code == 401


def test_a_second_change_revokes_the_first_sessions_tokens_again(db, client):
    user = make_caregiver(db).user
    first = client.post(CHANGE, json=body(), headers=auth_headers(user)).json()
    second = client.post(CHANGE, json=body(NEW_PASSWORD, "Another-Pass-77"), headers=bearer(first))
    assert second.status_code == 200
    assert client.get(ME, headers=bearer(first)).status_code == 401
    assert client.get(ME, headers=bearer(second.json())).status_code == 200


def test_wrong_current_password_counts_toward_a_backoff(db, client):
    user = make_caregiver(db).user
    headers = auth_headers(user)
    for _ in range(settings.CHANGE_PASSWORD_FREE_ATTEMPTS):  # the last of these starts the wait
        assert client.post(CHANGE, json=body(current="wrong"), headers=headers).status_code == 400
    r = client.post(CHANGE, json=body(), headers=headers)  # even the right password waits now
    assert r.status_code == 429
    assert error(r)["code"] == "too_many_attempts"
    assert int(r.headers["Retry-After"]) >= 1
    db.refresh(user)
    assert verify_password(DEFAULT_PASSWORD, user.hashed_password)  # the throttled call changed nothing


def test_the_wait_ends_and_success_clears_the_count(db, client):
    user = make_caregiver(db).user
    headers = auth_headers(user)
    for _ in range(settings.CHANGE_PASSWORD_FREE_ATTEMPTS):
        client.post(CHANGE, json=body(current="wrong"), headers=headers)
    key = f"cpw:{user.id}"
    row = db.scalars(select(AdminRateLimit).where(AdminRateLimit.key == key)).one()
    row.blocked_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    ok = client.post(CHANGE, json=body(), headers=headers)
    assert ok.status_code == 200
    assert db.scalars(select(AdminRateLimit).where(AdminRateLimit.key == key)).first() is None


def test_one_users_failures_do_not_throttle_another(db, client):
    a, b = make_caregiver(db).user, make_caregiver(db).user
    for _ in range(settings.CHANGE_PASSWORD_FREE_ATTEMPTS + 1):
        client.post(CHANGE, json=body(current="wrong"), headers=auth_headers(a))
    assert client.post(CHANGE, json=body(), headers=auth_headers(b)).status_code == 200


def test_weak_or_same_password_attempts_do_not_count_as_guesses(db, client):
    user = make_caregiver(db).user
    headers = auth_headers(user)
    for _ in range(settings.CHANGE_PASSWORD_FREE_ATTEMPTS + 3):
        assert client.post(CHANGE, json=body(new="short"), headers=headers).status_code == 400
    assert client.post(CHANGE, json=body(), headers=headers).status_code == 200


def test_no_password_or_token_reaches_the_logs(db, client, caplog):
    user = make_caregiver(db).user
    session = sign_in(client, user)
    with caplog.at_level(logging.DEBUG):
        client.post(CHANGE, json=body(current="wrong-current-secret"), headers=bearer(session))
        ok = client.post(CHANGE, json=body(), headers=bearer(session)).json()
    text = caplog.text
    for secret in ("wrong-current-secret", DEFAULT_PASSWORD, NEW_PASSWORD, ok["access_token"], ok["refresh_token"],
                   session["access_token"], session["refresh_token"]):
        assert secret not in text
    assert any(str(user.id) in rec.getMessage() for rec in caplog.records)  # the change itself is logged, by id only


def test_inactive_user_is_refused(db, client):
    user = make_caregiver(db).user
    headers = auth_headers(user)
    user.is_active = False
    db.commit()
    assert client.post(CHANGE, json=body(), headers=headers).status_code == 403


def test_login_and_refresh_issue_tokens_carrying_the_current_version(db, client):
    user = make_caregiver(db).user
    client.post(CHANGE, json=body(), headers=auth_headers(user))
    tokens = sign_in(client, user, NEW_PASSWORD)
    assert jwt.decode(tokens["access_token"], settings.SECRET_KEY, algorithms=["HS256"])["tv"] == 1
    refreshed = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}).json()
    assert jwt.decode(refreshed["refresh_token"], settings.SECRET_KEY, algorithms=["HS256"])["tv"] == 1


def test_a_token_minted_for_a_stale_version_is_refused(db, client):
    user = make_caregiver(db).user
    client.post(CHANGE, json=body(), headers=auth_headers(user))
    stale = create_access_token(user.id, token_version=0)
    assert client.get(ME, headers={"Authorization": f"Bearer {stale}"}).status_code == 401


def test_a_session_holding_a_stale_copy_of_the_user_still_sees_the_committed_change(db):
    """The auth dependency loads the user before the change takes its row lock; if another change commits in between,
    the second one must work from the committed hash and version, not from the stale copy it already holds."""
    from app.db.models.user import User
    from app.db.session import SessionLocal
    from app.services.change_password import ChangePasswordService

    user = make_caregiver(db).user
    other = SessionLocal()
    try:
        stale = other.get(User, user.id)
        assert stale.token_version == 0
        ChangePasswordService(db).change(user.id, DEFAULT_PASSWORD, NEW_PASSWORD)  # commits version 1
        changed = ChangePasswordService(other).change(user.id, NEW_PASSWORD, "Another-Pass-77")
        assert changed.token_version == 2
    finally:
        other.close()
