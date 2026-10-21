"""The permission catalogue, and every admin route against every role."""

import re
import uuid

import pytest
from sqlalchemy import text

from app.core.admin_permissions import ALL_PERMISSIONS, ROLE_NAMES, ROLE_PERMISSIONS
from app.main import app as fastapi_app
from app.tests.factories import auth_headers, make_admin

API = "/api/v1"

# Every route guarded by require_permission(...) or get_user_or_admin(...), with the permission it needs.
# test_every_guarded_route_is_listed fails when a route is added or re-guarded without updating this.
ADMIN_ONLY = {
    ("GET", f"{API}/admin/caregivers"): "caregivers.read",
    ("GET", f"{API}/admin/caregivers/{{caregiver_id}}"): "caregivers.read",
    ("POST", f"{API}/admin/caregivers/documents/{{doc_id}}/decision"): "caregivers.approve",
    ("POST", f"{API}/admin/caregivers/{{caregiver_id}}/identity-decision"): "caregivers.approve",
    ("POST", f"{API}/admin/caregivers/{{caregiver_id}}/background-check-decision"): "caregivers.approve",
    ("PUT", f"{API}/admin/caregivers/{{caregiver_id}}/skills"): "caregivers.approve",
    ("POST", f"{API}/admin/caregivers/{{caregiver_id}}/approve"): "caregivers.approve",
    ("POST", f"{API}/admin/caregivers/{{caregiver_id}}/reject"): "caregivers.approve",
    ("POST", f"{API}/admin/caregivers/{{caregiver_id}}/request-changes"): "caregivers.approve",
    ("POST", f"{API}/admin/caregivers/{{caregiver_id}}/suspend"): "caregivers.suspend",
    ("POST", f"{API}/admin/caregivers/{{caregiver_id}}/reinstate"): "caregivers.suspend",
    ("GET", f"{API}/admin/bookings"): "bookings.read",
    ("GET", f"{API}/admin/bookings/unmatched"): "bookings.read",
    ("GET", f"{API}/admin/bookings/{{booking_id}}"): "bookings.read",
    ("GET", f"{API}/admin/bookings/{{booking_id}}/candidates"): "bookings.assign",
    ("POST", f"{API}/admin/bookings/{{booking_id}}/redispatch"): "bookings.dispatch",
    ("POST", f"{API}/admin/bookings/{{booking_id}}/assign"): "bookings.assign",
    ("POST", f"{API}/admin/bookings/{{booking_id}}/reassign"): "bookings.assign",
    ("POST", f"{API}/admin/bookings/{{booking_id}}/cancel"): "bookings.cancel",
    ("POST", f"{API}/admin/bookings/{{booking_id}}/confirm-address"): "bookings.address",
    ("POST", f"{API}/admin/bookings/{{booking_id}}/no-show"): "bookings.dispatch",
    ("POST", f"{API}/admin/bookings/{{booking_id}}/correct-status"): "bookings.correct_status",
    ("GET", f"{API}/admin/booking-policy"): "bookings.read",
    ("PATCH", f"{API}/admin/booking-policy"): "settings.write",
    ("GET", f"{API}/booking-settings/"): "bookings.read",
    ("GET", f"{API}/booking-settings/latest"): "bookings.read",
    ("POST", f"{API}/booking-settings/"): "pricing.write",
    ("PUT", f"{API}/booking-settings/{{settings_id}}"): "pricing.write",
    ("DELETE", f"{API}/booking-settings/{{settings_id}}"): "pricing.write",
    ("POST", f"{API}/prescription/settings"): "prescriptions.manage",
    ("PUT", f"{API}/prescription/settings/{{settings_id}}"): "prescriptions.manage",
    ("DELETE", f"{API}/prescription/settings/{{settings_id}}"): "prescriptions.manage",
    ("PATCH", f"{API}/prescription/{{prescription_id}}"): "prescriptions.manage",
    ("POST", f"{API}/prescription/{{prescription_id}}/upload_note"): "prescriptions.manage",
    ("PATCH", f"{API}/prescription/{{prescription_id}}/status"): "prescriptions.manage",
    ("POST", f"{API}/fallguard/admin/question"): "fallguard.write",
    ("PUT", f"{API}/fallguard/admin/question/{{question_id}}"): "fallguard.write",
    ("DELETE", f"{API}/fallguard/admin/question/{{question_id}}"): "fallguard.write",
    ("GET", f"{API}/fallguard/admin/questions"): "fallguard.write",
    ("GET", f"{API}/patient/admin/refund/requests"): "refunds.approve",
    ("PUT", f"{API}/patient/admin/refund/requests/{{request_id}}"): "refunds.approve",
    ("GET", f"{API}/booking/ws/stats"): "dashboard.read",
    ("GET", f"{API}/admin/admins"): "admins.manage",
    ("POST", f"{API}/admin/admins"): "admins.manage",
    ("PATCH", f"{API}/admin/admins/{{admin_id}}"): "admins.manage",
    ("POST", f"{API}/admin/admins/{{admin_id}}/reset-mfa"): "admins.manage",
    ("GET", f"{API}/admin/audit"): "audit.read",
    ("GET", f"{API}/admin/earnings"): "payments.read",
    ("POST", f"{API}/admin/earnings/{{earning_id}}/hold"): "ledger.adjust_request",
    ("POST", f"{API}/admin/earnings/{{earning_id}}/release-hold"): "ledger.adjust_request",
    ("POST", f"{API}/admin/earnings/{{earning_id}}/void"): "ledger.adjust",
    ("GET", f"{API}/admin/payouts"): "payments.read",
    ("GET", f"{API}/admin/payouts/batches"): "payments.read",
    ("GET", f"{API}/admin/payouts/batches/{{batch_id}}"): "payments.read",
    ("POST", f"{API}/admin/payouts/batches/{{batch_id}}/approve"): "payouts.execute",
    ("POST", f"{API}/admin/payouts/{{payout_id}}/cancel"): "payouts.execute",
    ("POST", f"{API}/admin/payouts/{{payout_id}}/retry"): "ledger.adjust",
    ("POST", f"{API}/admin/payouts/{{payout_id}}/resolve"): "ledger.adjust",
    ("POST", f"{API}/admin/payouts/caregivers/{{caregiver_id}}/unfreeze"): "ledger.adjust",
    ("POST", f"{API}/admin/payouts/batches/{{batch_id}}/cancel"): "payouts.execute",
}
# Shared with app users: a patient/caregiver token works as before, an admin token needs the permission.
SHARED = {
    ("GET", f"{API}/booking/booking/{{booking_id}}"): "bookings.read",
    ("GET", f"{API}/booking/booking/{{booking_id}}/live-tracking"): "bookings.read",
    ("GET", f"{API}/caregiver/documents/{{document_id}}/file"): "documents.read",
    ("GET", f"{API}/prescription/patient/{{patient_id}}"): "prescriptions.manage",
    ("GET", f"{API}/prescription/{{prescription_id}}"): "prescriptions.manage",
    ("GET", f"{API}/prescription/{{prescription_id}}/status"): "prescriptions.manage",
}
ROLES = sorted(ROLE_PERMISSIONS)


def _admin_bearer_operations() -> set:
    """Every (method, path) the OpenAPI document marks as needing an admin token."""
    spec = fastapi_app.openapi()
    return {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if any("AdminBearer" in scheme for scheme in op.get("security", []))
    }


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid.uuid4()), path)


def test_every_admin_route_is_in_the_matrix():
    # A route that needs an admin token but is missing from ADMIN_ONLY (or the reverse) fails here, so
    # a new admin route cannot ship without being added to the permission matrix below.
    assert _admin_bearer_operations() == set(ADMIN_ONLY) | {("GET", f"{API}/admin/me")}


def test_every_path_under_admin_needs_an_admin_token_except_sign_in():
    spec = fastapi_app.openapi()
    open_paths = [p for p in spec["paths"] if "/admin/" in p and not p.startswith(f"{API}/admin/auth/")]
    needing = {path for _m, path in _admin_bearer_operations()}
    assert [p for p in open_paths if p not in needing] == []


def test_every_permission_used_exists_and_is_granted_to_super_admin():
    used = set(ADMIN_ONLY.values()) | set(SHARED.values())
    assert used <= ALL_PERMISSIONS
    assert ROLE_PERMISSIONS["super_admin"] == ALL_PERMISSIONS


def test_catalogue_in_code_matches_the_seeded_database(db):
    rows = db.execute(text(
        "SELECT r.key, r.name, p.permission FROM admin_roles r "
        "LEFT JOIN admin_role_permissions p ON p.role_id = r.id")).all()
    names = {k: n for k, n, _ in rows}
    perms = {}
    for key, _name, perm in rows:
        perms.setdefault(key, set()).add(perm)
    assert names == ROLE_NAMES
    assert perms == {k: set(v) for k, v in ROLE_PERMISSIONS.items()}


def test_prd_spot_checks():
    p = ROLE_PERMISSIONS
    assert "caregivers.approve" in p["verification"] and "caregivers.approve" not in p["operations"]
    assert "caregivers.suspend" in p["operations"]  # PRD: Ops may suspend but not approve
    assert "refunds.approve" in p["finance"] and "refunds.approve" not in p["support"]
    assert "bookings.cancel_request" in p["support"] and "bookings.cancel" not in p["support"]
    assert p["read_only_analyst"] == {"dashboard.read"}
    assert "clinical.read" in p["clinical_reviewer"] and "clinical.read" not in p["operations"]
    assert "admins.manage" not in set().union(*(v for k, v in p.items() if k != "super_admin"))
    # Finance reads the audit trail of the payouts it approves (owner decision 2026-10-19); nobody else but the Super Admin does
    assert "audit.read" in p["finance"]
    assert "audit.read" not in set().union(*(v for k, v in p.items() if k not in ("super_admin", "finance")))
    assert "admins.manage" not in p["finance"]


def test_only_super_admin_can_approve_above_limit():
    holders = {k for k, v in ROLE_PERMISSIONS.items() if "refunds.approve_above_limit" in v}
    assert holders == {"super_admin"}


@pytest.fixture
def role_headers(db):
    # one admin per role; password=None skips bcrypt, nothing here signs in with a password
    return {role: auth_headers(make_admin(db, [role], password=None)) for role in ROLES}


@pytest.mark.parametrize("route", sorted(ADMIN_ONLY), ids=lambda r: f"{r[0]} {r[1]}")
def test_admin_route_matrix(client, role_headers, route):
    method, template = route
    permission = ADMIN_ONLY[route]
    url = _fill(template)

    assert client.request(method, url).status_code == 401  # no token
    for role in ROLES:
        r = client.request(method, url, headers=role_headers[role])
        if permission in ROLE_PERMISSIONS[role]:
            assert r.status_code not in (401, 403), (role, r.status_code, r.text)
        else:
            assert r.status_code == 403, (role, r.status_code, r.text)
            assert permission in r.json()["detail"]


@pytest.mark.parametrize("route", sorted(SHARED), ids=lambda r: f"{r[0]} {r[1]}")
def test_shared_route_matrix(client, role_headers, route):
    method, template = route
    permission = SHARED[route]
    url = _fill(template)

    assert client.request(method, url).status_code == 401
    for role in ROLES:
        r = client.request(method, url, headers=role_headers[role])
        if permission in ROLE_PERMISSIONS[role]:
            assert r.status_code not in (401, 403), (role, r.status_code, r.text)  # 404: nothing there
        else:
            assert r.status_code == 403, (role, r.status_code, r.text)


def test_finance_reads_the_audit_log_but_cannot_manage_admins(db, client):
    finance = auth_headers(make_admin(db, ["finance"], password=None))
    boss = make_admin(db, ["super_admin"], password=None)
    r = client.get(f"{API}/admin/audit", headers=finance)
    assert r.status_code == 200 and set(r.json()) == {"items", "total", "limit", "offset"}
    # read-only: the audit route has no write verbs, and admin management stays closed to Finance
    assert client.post(f"{API}/admin/audit", headers=finance, json={}).status_code == 405
    assert client.get(f"{API}/admin/admins", headers=finance).status_code == 403
    assert client.post(f"{API}/admin/admins", headers=finance, json={"email": "x@example.com", "full_name": "X", "roles": ["super_admin"]}).status_code == 403
    assert client.patch(f"{API}/admin/admins/{boss.id}", headers=finance, json={"roles": []}).status_code == 403
    # no other role gained it
    for role in ("operations", "verification", "support", "clinical_reviewer", "read_only_analyst"):
        assert client.get(f"{API}/admin/audit", headers=auth_headers(make_admin(db, [role], password=None))).status_code == 403
