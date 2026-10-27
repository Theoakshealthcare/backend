"""``get_user_or_admin``: the dependency behind the few routes shared by app users and the console.

It must pick the right decoder without ever trusting an unverified token: an app-user token never
authenticates as an admin, an admin token never authenticates as a user, and expired, forged or
malformed tokens are refused. The probe app below isolates the dependency from any one route.
"""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.deps import get_user_or_admin, is_admin
from app.core.admin_security import create_admin_token
from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token
from app.db.models.admin import AdminRefreshToken
from app.tests.factories import admin_session, auth_headers, make_admin, make_patient

PERMISSION = "bookings.read"

probe = FastAPI()


@probe.get("/probe")
def _probe(actor=Depends(get_user_or_admin(PERMISSION))) -> dict:
    return {"kind": "admin" if is_admin(actor) else "user", "id": str(actor.id)}


@pytest.fixture
def probe_client():
    with TestClient(probe, raise_server_exceptions=False) as c:
        yield c


def bearer_token(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def claims(subject, **extra) -> dict:
    now = datetime.now(timezone.utc)
    return {"sub": str(subject), "iat": now, "exp": now + timedelta(minutes=5), **extra}


# --- valid tokens behave exactly as before -----------------------------------------------------


def test_an_app_user_token_authenticates_as_that_user(db, probe_client):
    patient = make_patient(db)
    r = probe_client.get("/probe", headers=auth_headers(patient.user))
    assert r.status_code == 200 and r.json() == {"kind": "user", "id": str(patient.user.id)}


def test_an_admin_token_with_the_permission_authenticates_as_that_admin(db, probe_client):
    admin = make_admin(db, roles=("finance",))
    r = probe_client.get("/probe", headers=auth_headers(admin))
    assert r.status_code == 200 and r.json() == {"kind": "admin", "id": str(admin.id)}


def test_an_admin_without_the_permission_is_403_not_a_user(db, probe_client):
    admin = make_admin(db, roles=("verification",))  # holds no bookings.read
    r = probe_client.get("/probe", headers=auth_headers(admin))
    assert r.status_code == 403
    assert PERMISSION in r.json()["detail"]


def test_no_token_is_401(probe_client):
    assert probe_client.get("/probe").status_code == 401


# --- the two token kinds never cross -----------------------------------------------------------


def test_an_app_token_for_an_admins_id_never_authenticates_as_that_admin(db, probe_client):
    admin = make_admin(db)
    r = probe_client.get("/probe", headers=bearer_token(create_access_token(admin.id)))
    assert r.status_code == 401


def test_an_admin_token_for_a_users_id_never_authenticates_as_that_user(db, probe_client):
    patient = make_patient(db)
    token = create_admin_token(patient.user.id, "access", timedelta(minutes=5), sid="00000000-0000-0000-0000-000000000000")
    assert probe_client.get("/probe", headers=bearer_token(token)).status_code == 401


def test_an_app_signed_token_claiming_the_admin_audience_is_401(db, probe_client):
    admin = make_admin(db)
    forged = pyjwt.encode(claims(admin.id, aud="admin", type="access", sid="x"), settings.SECRET_KEY, algorithm="HS256")
    assert probe_client.get("/probe", headers=bearer_token(forged)).status_code == 401


def test_an_admin_signed_token_without_the_audience_is_401(db, probe_client):
    from app.core.admin_security import _signing_key

    admin = make_admin(db)
    forged = pyjwt.encode(claims(admin.id, type="access"), _signing_key(), algorithm="HS256")
    assert probe_client.get("/probe", headers=bearer_token(forged)).status_code == 401


# --- expired, forged and malformed tokens ------------------------------------------------------


def test_expired_tokens_are_401(db, probe_client):
    patient, admin = make_patient(db), make_admin(db)
    _access, _refresh = admin_session(db, admin)
    expired_admin = create_admin_token(admin.id, "access", timedelta(seconds=-5), sid="x")
    expired_user = pyjwt.encode(
        {"sub": str(patient.user.id), "type": "access", "exp": datetime.now(timezone.utc) - timedelta(seconds=5)},
        settings.SECRET_KEY, algorithm="HS256",
    )
    assert probe_client.get("/probe", headers=bearer_token(expired_admin)).status_code == 401
    assert probe_client.get("/probe", headers=bearer_token(expired_user)).status_code == 401


def test_tokens_signed_with_the_wrong_key_are_401(db, probe_client):
    patient, admin = make_patient(db), make_admin(db)
    bad_user = pyjwt.encode(claims(patient.user.id, type="access"), "x" * 40, algorithm="HS256")
    bad_admin = pyjwt.encode(claims(admin.id, aud="admin", type="access", sid="x"), "x" * 40, algorithm="HS256")
    assert probe_client.get("/probe", headers=bearer_token(bad_user)).status_code == 401
    assert probe_client.get("/probe", headers=bearer_token(bad_admin)).status_code == 401


@pytest.mark.parametrize("payload", [{"type": "access"}, {"type": "access", "aud": "admin", "sid": "x"}])
def test_unsigned_alg_none_tokens_are_401(db, probe_client, payload):
    subject = make_admin(db).id
    token = pyjwt.encode(claims(subject, **payload), None, algorithm="none")
    assert probe_client.get("/probe", headers=bearer_token(token)).status_code == 401


def test_a_tampered_signature_is_401(db, probe_client):
    patient = make_patient(db)
    token = auth_headers(patient.user)["Authorization"].split()[1]
    head, body, sig = token.split(".")
    tampered = ".".join([head, body, ("A" if sig[0] != "A" else "B") + sig[1:]])
    assert probe_client.get("/probe", headers=bearer_token(tampered)).status_code == 401


@pytest.mark.parametrize("junk", ["not-a-jwt", "a.b.c", "....", "Bearer"])
def test_malformed_tokens_are_401(probe_client, junk):
    assert probe_client.get("/probe", headers=bearer_token(junk)).status_code == 401


def test_refresh_tokens_are_401(db, probe_client):
    patient, admin = make_patient(db), make_admin(db)
    admin_refresh = create_admin_token(admin.id, "refresh", timedelta(minutes=5), sid="x")
    assert probe_client.get("/probe", headers=bearer_token(create_refresh_token(patient.user.id))).status_code == 401
    assert probe_client.get("/probe", headers=bearer_token(admin_refresh)).status_code == 401


def test_an_admin_token_whose_session_was_revoked_is_401(db, probe_client):
    admin = make_admin(db)
    headers = auth_headers(admin)
    assert probe_client.get("/probe", headers=headers).status_code == 200
    db.execute(update(AdminRefreshToken).where(AdminRefreshToken.admin_user_id == admin.id)
               .values(revoked_at=datetime.utcnow()))
    db.commit()
    assert probe_client.get("/probe", headers=headers).status_code == 401


def test_a_disabled_admin_is_401_and_an_inactive_user_is_403(db, probe_client):
    admin = make_admin(db, status="disabled")
    assert probe_client.get("/probe", headers=auth_headers(admin)).status_code == 401
    patient = make_patient(db)
    headers = auth_headers(patient.user)
    patient.user.is_active = False
    db.commit()
    r = probe_client.get("/probe", headers=headers)
    assert r.status_code == 403 and r.json()["detail"] == "Inactive user"


# --- no unverified decode ----------------------------------------------------------------------


def test_no_token_is_ever_decoded_without_verification(db, probe_client, monkeypatch):
    """Valid tokens of both kinds, and a bad one, are resolved without a ``verify_signature=False`` decode."""
    real_decode = pyjwt.decode

    def guarded(token, *args, **kwargs):
        options = kwargs.get("options") or {}
        assert options.get("verify_signature", True), "unverified JWT decode"
        assert len(args) >= 1 or "key" in kwargs, "decoded without a key"
        return real_decode(token, *args, **kwargs)

    monkeypatch.setattr(pyjwt, "decode", guarded)
    patient, admin = make_patient(db), make_admin(db, roles=("finance",))
    assert probe_client.get("/probe", headers=auth_headers(patient.user)).status_code == 200
    assert probe_client.get("/probe", headers=auth_headers(admin)).status_code == 200
    assert probe_client.get("/probe", headers=bearer_token("a.b.c")).status_code == 401
