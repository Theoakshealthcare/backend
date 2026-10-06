"""App-user JWTs (PyJWT): algorithm, required claims, token types and the audience boundary."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.admin_security import create_admin_token
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    generate_password_reset_token,
    verify_password_reset_token,
    verify_refresh_token,
)
from app.tests.factories import auth_headers, make_admin, make_patient

ME = "/api/v1/auth/me"


def _encode(claims: dict, key: str = None, alg: str = "HS256") -> str:
    return jwt.encode(claims, key or settings.SECRET_KEY, algorithm=alg)


def _claims(sub: str, **extra) -> dict:
    return {"sub": sub, "type": "access", "exp": datetime.now(timezone.utc) + timedelta(minutes=5), **extra}


def test_issued_tokens_are_hs256_with_required_claims():
    for token, kind in ((create_access_token("u1"), "access"), (create_refresh_token("u1"), "refresh")):
        assert jwt.get_unverified_header(token)["alg"] == "HS256"
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        assert claims["sub"] == "u1" and claims["type"] == kind and "exp" in claims and "aud" not in claims


@pytest.mark.parametrize("kind", ["hs512", "no_exp", "no_sub", "expired", "wrong_key", "aud_claim", "admin_token"])
def test_bad_app_tokens_are_401(db, client, kind):
    patient = make_patient(db)
    sub = str(patient.user.id)
    token = {
        "hs512": _encode(_claims(sub), alg="HS512"),
        "no_exp": _encode({"sub": sub, "type": "access"}),
        "no_sub": _encode({"type": "access", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}),
        "expired": _encode(_claims(sub, exp=datetime.now(timezone.utc) - timedelta(seconds=5))),
        "wrong_key": _encode(_claims(sub), key="not-the-secret-key-not-the-secret-key"),
        "aud_claim": _encode(_claims(sub, aud="admin")),
        "admin_token": create_admin_token(patient.user.id, "access", timedelta(minutes=5), sid=sub),
    }[kind]
    r = client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_unsigned_token_is_refused(db, client):
    patient = make_patient(db)
    token = jwt.encode(_claims(str(patient.user.id)), key=None, algorithm="none")
    assert client.get(ME, headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_app_token_is_refused_on_admin_routes(db, client):
    patient = make_patient(db)
    assert client.get("/api/v1/admin/me", headers=auth_headers(patient.user)).status_code == 401


def test_admin_token_is_refused_on_app_routes(db, client):
    admin = make_admin(db)
    assert client.get(ME, headers=auth_headers(admin)).status_code == 401


def test_refresh_verifier_only_accepts_refresh_tokens():
    assert verify_refresh_token(create_refresh_token("u1")) == "u1"
    assert verify_refresh_token(create_access_token("u1")) is None
    assert verify_refresh_token(generate_password_reset_token("a@b.test")) is None
    assert verify_refresh_token("garbage") is None


def test_password_reset_token_round_trip_and_type_separation():
    token = generate_password_reset_token("a@b.test")
    assert verify_password_reset_token(token) == "a@b.test"
    assert verify_password_reset_token(create_access_token("a@b.test")) is None
    assert verify_password_reset_token(create_refresh_token("a@b.test")) is None
    assert verify_password_reset_token("garbage") is None


def test_reset_token_cannot_authenticate_api_calls(db, client):
    patient = make_patient(db)
    token = generate_password_reset_token(str(patient.user.id))
    assert client.get(ME, headers={"Authorization": f"Bearer {token}"}).status_code == 401
