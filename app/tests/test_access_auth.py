"""WP-C items 1 and 3: token type, inactive users, and removed public diagnostics."""

from datetime import datetime, timedelta

import jwt

from app.core.config import settings
from app.tests.factories import (
    auth_headers,
    make_admin,
    make_caregiver,
    make_patient,
    make_user,
    refresh_headers,
)

ME = "/api/v1/auth/me"


def test_me_without_token_is_401(client):
    assert client.get(ME).status_code == 401


def test_me_with_access_token_is_200(db, client):
    patient = make_patient(db)
    assert client.get(ME, headers=auth_headers(patient.user)).status_code == 200


def test_refresh_token_is_rejected_as_access_token(db, client):
    patient = make_patient(db)
    r = client.get(ME, headers=refresh_headers(patient.user))
    assert r.status_code == 401


def test_refresh_token_is_rejected_on_a_role_guarded_route(db, client):
    caregiver = make_caregiver(db)
    r = client.get("/api/v1/caregiver/profile", headers=refresh_headers(caregiver.user))
    assert r.status_code == 401


def test_token_without_type_claim_is_rejected(db, client):
    patient = make_patient(db)
    token = jwt.encode(
        {"sub": str(patient.user.id), "exp": datetime.utcnow() + timedelta(minutes=5)},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    r = client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_inactive_user_is_refused_everywhere(db, client):
    patient = make_patient(db)
    patient.user.is_active = False
    db.commit()
    headers = auth_headers(patient.user)
    for path in (ME, f"/api/v1/prescription/patient/{patient.id}"):
        assert client.get(path, headers=headers).status_code in (401, 403), path


def test_inactive_caregiver_cannot_use_caregiver_routes(db, client):
    caregiver = make_caregiver(db)
    caregiver.user.is_active = False
    db.commit()
    r = client.get("/api/v1/caregiver/profile", headers=auth_headers(caregiver.user))
    assert r.status_code in (401, 403)


# --- /booking/ws/stats is admin-only -----------------------------------------

STATS = "/api/v1/booking/ws/stats"


def test_ws_stats_requires_token(client):
    assert client.get(STATS).status_code == 401


def test_ws_stats_rejects_app_user_tokens(db, client):
    # an app-user token is not an admin credential at all: 401, not 403
    patient = make_patient(db)
    assert client.get(STATS, headers=auth_headers(patient.user)).status_code == 401


def test_ws_stats_allowed_for_admin(db, client):
    admin = make_admin(db)
    r = client.get(STATS, headers=auth_headers(admin))
    assert r.status_code == 200
    assert "active_caregiver_connections" in r.json()


# --- /auth/test-cloudinary is gone -------------------------------------------


def test_test_cloudinary_endpoint_is_removed(db, client):
    user = make_user(db)
    assert client.get("/api/v1/auth/test-cloudinary").status_code in (404, 405)
    assert client.get("/api/v1/auth/test-cloudinary", headers=auth_headers(user)).status_code in (404, 405)
