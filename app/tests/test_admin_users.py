"""Admin management: invites, roles, disabling, the last Super Admin, MFA reset, and the audit viewer."""

import threading
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.admin_security import hash_token
from app.db.models.admin import AdminUser
from app.db.models.audit import AdminAuditLog
from app.db.session import SessionLocal
from app.services.admin_users import AdminConflict, AdminUserService
from app.tests.admin_helpers import (
    ACCEPT,
    ADMINS,
    AUDIT,
    LOGIN,
    ME,
    REFRESH,
    VERIFY,
    admin_login,
    bearer,
    fake_request,
)
from app.tests.factories import admin_totp_secret, auth_headers, make_admin

STRONG = "Correct-Horse-9-Battery"  # placeholder test password


def _invite(client, headers, email="new.admin@example.com", roles=("operations",), name="New Admin"):
    return client.post(ADMINS, headers=headers, json={"email": email, "full_name": name, "roles": list(roles)})


def _fresh(db, admin_id):
    db.expire_all()
    return db.get(AdminUser, uuid.UUID(str(admin_id)))


# --- invite ----------------------------------------------------------------------------------


def test_invite_creates_an_invited_admin_and_returns_a_single_use_token(db, client):
    boss = make_admin(db)
    r = _invite(client, auth_headers(boss), email="New.Admin@Example.com", roles=["operations", "support"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "new.admin@example.com"
    assert body["status"] == "invited" and body["roles"] == ["operations", "support"]
    assert body["mfa_enabled"] is False
    assert len(body["invite_token"]) >= 40
    expires = datetime.fromisoformat(body["invite_expires_at"])
    assert timedelta(hours=47, minutes=59) < expires - datetime.utcnow() <= timedelta(hours=48)

    row = _fresh(db, body["id"])
    assert row.created_by == boss.id and row.password_hash is None
    assert row.invite_token_hash == hash_token(body["invite_token"])
    assert body["invite_token"] not in str(row.__dict__)  # only the hash is stored


def test_invite_needs_admins_manage(db, client):
    support = make_admin(db, ["support"])
    assert _invite(client, auth_headers(support)).status_code == 403
    assert _invite(client, {}).status_code == 401


def test_invite_duplicate_email_is_409(db, client):
    boss = make_admin(db, email="boss@example.com")
    r = _invite(client, auth_headers(boss), email="BOSS@example.com")
    assert r.status_code == 409 and r.json() == {"detail": "An admin with this email already exists"}


def test_invite_unknown_role_is_422(db, client):
    r = _invite(client, auth_headers(make_admin(db)), roles=["wizard"])
    assert r.status_code == 422 and "wizard" in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {"email": "not-an-email", "full_name": "A", "roles": ["support"]},
    {"email": "a@example.com", "full_name": "", "roles": ["support"]},
    {"email": "a@example.com", "full_name": "A", "roles": []},
    {"email": "a@example.com", "full_name": "A"},
])
def test_invite_validates_the_body(db, client, body):
    assert client.post(ADMINS, headers=auth_headers(make_admin(db)), json=body).status_code == 422


def test_accept_invite_sets_a_password_and_forces_mfa_enrolment(db, client, admin_clock):
    boss = make_admin(db)
    invite = _invite(client, auth_headers(boss), email="fresh@example.com").json()

    # cannot sign in before accepting
    assert client.post(LOGIN, json={"email": "fresh@example.com", "password": STRONG}).status_code == 401

    r = client.post(ACCEPT, json={"invite_token": invite["invite_token"], "password": STRONG})
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": True, "email": "fresh@example.com"}
    row = _fresh(db, invite["id"])
    assert row.status == "active" and row.invite_token_hash is None and row.invite_expires_at is None
    assert row.password_hash and STRONG not in row.password_hash

    login = client.post(LOGIN, json={"email": "fresh@example.com", "password": STRONG})
    assert login.status_code == 200 and login.json()["mfa_enrollment_required"] is True
    ok = client.post(VERIFY, json={"mfa_token": login.json()["mfa_token"],
                                   "code": admin_clock.code(login.json()["secret"])})
    assert ok.status_code == 200
    assert ok.json()["admin"]["roles"] == ["operations"]
    assert "admin.invite_accepted" in [a.action for a in db.scalars(select(AdminAuditLog))]


def test_an_invite_can_only_be_used_once(db, client):
    invite = _invite(client, auth_headers(make_admin(db))).json()
    first = client.post(ACCEPT, json={"invite_token": invite["invite_token"], "password": STRONG})
    again = client.post(ACCEPT, json={"invite_token": invite["invite_token"], "password": "Another-Strong-Pass-1"})
    assert first.status_code == 200
    assert again.status_code == 400 and again.json() == {"detail": "Invite link is invalid or has expired"}
    # the second attempt did not change the password
    assert client.post(LOGIN, json={"email": "new.admin@example.com", "password": STRONG}).status_code == 200


def test_concurrent_accepts_of_one_invite_succeed_once(db, client):
    from app.services.admin_auth import AdminAuthService, InvalidInvite

    invite = _invite(client, auth_headers(make_admin(db))).json()
    barrier = threading.Barrier(2)
    results = []

    def attempt(password):
        session = SessionLocal()
        try:
            barrier.wait()
            AdminAuthService(session).accept_invite(invite["invite_token"], password, fake_request())
            results.append("ok")
        except InvalidInvite:
            results.append("refused")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(p,)) for p in (STRONG, "Another-Strong-Pass-1")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok", "refused"]


def test_an_expired_invite_is_refused(db, client):
    invite = _invite(client, auth_headers(make_admin(db))).json()
    row = _fresh(db, invite["id"])
    row.invite_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    r = client.post(ACCEPT, json={"invite_token": invite["invite_token"], "password": STRONG})
    assert r.status_code == 400
    assert _fresh(db, invite["id"]).status == "invited"


def test_unknown_invite_token_is_400(client):
    r = client.post(ACCEPT, json={"invite_token": "nope", "password": STRONG})
    assert r.status_code == 400


@pytest.mark.parametrize("password", ["short1!", "aaaaaaaaaaaaaaaa", "new.admin-is-me-1234"])
def test_weak_passwords_are_refused_and_the_invite_survives(db, client, password):
    invite = _invite(client, auth_headers(make_admin(db))).json()
    r = client.post(ACCEPT, json={"invite_token": invite["invite_token"], "password": password})
    assert r.status_code == 422
    assert _fresh(db, invite["id"]).status == "invited"
    assert client.post(ACCEPT, json={"invite_token": invite["invite_token"], "password": STRONG}).status_code == 200


def test_a_disabled_invite_cannot_be_accepted(db, client):
    boss = make_admin(db)
    invite = _invite(client, auth_headers(boss)).json()
    r = client.patch(f"{ADMINS}/{invite['id']}", headers=auth_headers(boss), json={"status": "disabled"})
    assert r.status_code == 200
    assert client.post(ACCEPT, json={"invite_token": invite["invite_token"], "password": STRONG}).status_code == 400


# --- list, update ------------------------------------------------------------------------------


def test_list_admins(db, client):
    boss = make_admin(db, email="boss@example.com")
    make_admin(db, ["finance"], email="fin@example.com", status="disabled", mfa=False)
    r = client.get(ADMINS, headers=auth_headers(boss))
    assert r.status_code == 200
    by_email = {a["email"]: a for a in r.json()}
    assert set(by_email) == {"boss@example.com", "fin@example.com"}
    assert by_email["fin@example.com"]["status"] == "disabled"
    assert by_email["fin@example.com"]["roles"] == ["finance"]
    assert by_email["boss@example.com"]["mfa_enabled"] is True
    assert set(by_email["boss@example.com"]) == {
        "id", "email", "full_name", "status", "mfa_enabled", "roles", "last_login_at", "locked_until", "created_at"}
    assert "password_hash" not in r.text and "mfa_secret" not in r.text and "invite_token_hash" not in r.text


def test_update_roles_takes_effect_immediately_and_is_audited(db, client):
    boss, target = make_admin(db), make_admin(db, ["support"])
    r = client.patch(f"{ADMINS}/{target.id}", headers=auth_headers(boss), json={"roles": ["finance", "support"]})
    assert r.status_code == 200 and r.json()["roles"] == ["finance", "support"]
    me = client.get(ME, headers=auth_headers(target))
    assert "refunds.approve" in me.json()["permissions"]
    row = db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "admin.update")).one()
    assert row.admin_user_id == boss.id and row.entity_id == str(target.id)
    assert row.before["roles"] == ["support"] and row.after["roles"] == ["finance", "support"]


def test_disabling_revokes_sessions_and_blocks_login(db, client, admin_clock):
    boss, target = make_admin(db), make_admin(db, ["support"])
    session = admin_login(client, admin_clock, target)
    r = client.patch(f"{ADMINS}/{target.id}", headers=auth_headers(boss), json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert client.get(ME, headers=bearer(session["access_token"])).status_code == 401
    assert client.post(REFRESH, json={"refresh_token": session["refresh_token"]}).status_code == 401
    assert client.post(LOGIN, json={"email": target.email, "password": "Passw0rd!test"}).status_code == 401

    # re-enabling restores sign-in
    client.patch(f"{ADMINS}/{target.id}", headers=auth_headers(boss), json={"status": "active"})
    assert client.post(LOGIN, json={"email": target.email, "password": "Passw0rd!test"}).status_code == 200


def test_cannot_activate_an_admin_who_never_accepted(db, client):
    boss = make_admin(db)
    invite = _invite(client, auth_headers(boss)).json()
    client.patch(f"{ADMINS}/{invite['id']}", headers=auth_headers(boss), json={"status": "disabled"})
    r = client.patch(f"{ADMINS}/{invite['id']}", headers=auth_headers(boss), json={"status": "active"})
    assert r.status_code == 409


def test_cannot_disable_yourself(db, client):
    boss = make_admin(db)
    make_admin(db)  # a second super admin, so the last-one rule is not what refuses
    r = client.patch(f"{ADMINS}/{boss.id}", headers=auth_headers(boss), json={"status": "disabled"})
    assert r.status_code == 409 and "own account" in r.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"status": "invited"}, {"roles": []}, {"status": "nope"}])
def test_update_validates_the_body(db, client, body):
    boss, target = make_admin(db), make_admin(db, ["support"])
    r = client.patch(f"{ADMINS}/{target.id}", headers=auth_headers(boss), json=body)
    assert r.status_code in (200, 422)  # {} is a no-op; the rest are invalid
    if body:
        assert r.status_code == 422


def test_update_unknown_admin_is_404(db, client):
    r = client.patch(f"{ADMINS}/{uuid.uuid4()}", headers=auth_headers(make_admin(db)), json={"status": "disabled"})
    assert r.status_code == 404


# --- last super admin ----------------------------------------------------------------------------


def test_the_last_super_admin_cannot_be_disabled_or_demoted(db, client):
    only = make_admin(db)
    other = make_admin(db, ["operations"])
    # the only Super Admin acting on themselves: 'disable yourself' fires first, so use roles
    r = client.patch(f"{ADMINS}/{only.id}", headers=auth_headers(only), json={"roles": ["operations"]})
    assert r.status_code == 409
    assert r.json() == {"detail": "Cannot remove or disable the last active Super Admin"}
    assert _fresh(db, only.id).role_keys == ["super_admin"]
    assert other.status == "active"


def test_a_super_admin_can_be_removed_while_another_remains(db, client):
    a, b = make_admin(db), make_admin(db)
    r = client.patch(f"{ADMINS}/{b.id}", headers=auth_headers(a), json={"status": "disabled"})
    assert r.status_code == 200
    # a is now the last one
    r = client.patch(f"{ADMINS}/{a.id}", headers=auth_headers(a), json={"roles": ["support"]})
    assert r.status_code == 409


def test_disabled_super_admins_do_not_count(db, client):
    a = make_admin(db)
    make_admin(db, status="disabled")  # a Super Admin in name only
    r = client.patch(f"{ADMINS}/{a.id}", headers=auth_headers(a), json={"roles": ["support"]})
    assert r.status_code == 409


def test_two_super_admins_cannot_remove_each_other_at_once(db):
    # Each change alone is fine (the other Super Admin remains). Without the advisory lock both
    # could read "someone else is still active" and leave none.
    a, b = make_admin(db), make_admin(db)
    barrier = threading.Barrier(2)
    outcome = {}

    def remove(actor_id, target_id, key):
        session = SessionLocal()
        try:
            service = AdminUserService(session)
            actor = session.get(AdminUser, actor_id)
            barrier.wait()
            service.update(actor, target_id, None, "disabled", fake_request())
            outcome[key] = "ok"
        except AdminConflict:
            session.rollback()
            outcome[key] = "refused"
        finally:
            session.close()

    threads = [threading.Thread(target=remove, args=(a.id, b.id, "a_removes_b")),
               threading.Thread(target=remove, args=(b.id, a.id, "b_removes_a"))]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(outcome.values()) == ["ok", "refused"], outcome
    db.expire_all()
    assert sum(1 for x in (db.get(AdminUser, a.id), db.get(AdminUser, b.id)) if x.status == "active") == 1


# --- reset MFA ---------------------------------------------------------------------------------


def test_reset_mfa_forces_re_enrolment_and_ends_sessions(db, client, admin_clock):
    boss, target = make_admin(db), make_admin(db, ["support"])
    old_secret = admin_totp_secret(target)
    session = admin_login(client, admin_clock, target)

    r = client.post(f"{ADMINS}/{target.id}/reset-mfa", headers=auth_headers(boss))
    assert r.status_code == 200 and r.json()["mfa_enabled"] is False
    row = _fresh(db, target.id)
    assert row.mfa_secret_enc is None and row.mfa_last_step is None
    assert client.get(ME, headers=bearer(session["access_token"])).status_code == 401

    login = client.post(LOGIN, json={"email": target.email, "password": "Passw0rd!test"}).json()
    assert login["mfa_enrollment_required"] is True
    assert login["secret"] != old_secret
    # the old authenticator no longer works
    admin_clock.advance(30)
    bad = client.post(VERIFY, json={"mfa_token": login["mfa_token"], "code": admin_clock.code(old_secret)})
    assert bad.status_code == 401
    ok = client.post(VERIFY, json={"mfa_token": login["mfa_token"], "code": admin_clock.code(login["secret"])})
    assert ok.status_code == 200
    assert "admin.reset_mfa" in [a.action for a in db.scalars(select(AdminAuditLog))]


def test_reset_mfa_lifts_a_lockout_and_needs_permission(db, client):
    boss, target = make_admin(db), make_admin(db, ["support"])
    target.locked_until = datetime.utcnow() + timedelta(minutes=10)
    db.commit()
    assert client.post(f"{ADMINS}/{target.id}/reset-mfa", headers=auth_headers(target)).status_code == 403
    client.post(f"{ADMINS}/{target.id}/reset-mfa", headers=auth_headers(boss))
    assert _fresh(db, target.id).locked_until is None


def test_reset_mfa_unknown_admin_is_404(db, client):
    assert client.post(f"{ADMINS}/{uuid.uuid4()}/reset-mfa", headers=auth_headers(make_admin(db))).status_code == 404


# --- audit rows reference admin_users, and the viewer -----------------------------------------------


def test_existing_admin_actions_write_audit_rows_for_the_admin_user(db, client):
    from app.tests.factories import make_caregiver

    admin = make_admin(db)
    cg = make_caregiver(db, approved=False)
    r = client.post(f"/api/v1/admin/caregivers/{cg.id}/reject", json={"reason": "blurry licence"},
                    headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    row = db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "caregiver.reject")).one()
    assert row.admin_user_id == admin.id


def _seed_audit(db, boss, other):
    svc = __import__("app.services.audit", fromlist=["AuditService"]).AuditService(db)
    svc.record(admin_user_id=boss.id, action="caregiver.approve", entity_type="caregiver", entity_id="c1")
    svc.record(admin_user_id=boss.id, action="caregiver.reject", entity_type="caregiver", entity_id="c2",
               reason="blurry", after={"password": "should-not-appear"})
    svc.record(admin_user_id=other.id, action="refund.approve", entity_type="refund", entity_id="r1")
    db.commit()


def test_audit_viewer_filters_and_paginates(db, client):
    boss, other = make_admin(db, email="boss@example.com"), make_admin(db, ["finance"], email="fin@example.com")
    _seed_audit(db, boss, other)
    h = auth_headers(boss)

    r = client.get(AUDIT, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
    assert body["items"][0]["created_at"] >= body["items"][-1]["created_at"]  # newest first
    assert {i["admin_email"] for i in body["items"]} == {"boss@example.com", "fin@example.com"}

    def total(**params):
        return client.get(AUDIT, headers=h, params=params).json()["total"]

    assert total(entity_type="caregiver") == 2
    assert total(entity_type="caregiver", entity_id="c2") == 1
    assert total(admin_user_id=str(other.id)) == 1
    assert total(action="refund.approve") == 1
    today = datetime.utcnow().date()
    assert total(date_from=today.isoformat(), date_to=today.isoformat()) == 3
    assert total(date_from=(today + timedelta(days=1)).isoformat()) == 0
    assert total(date_to=(today - timedelta(days=1)).isoformat()) == 0

    page = client.get(AUDIT, headers=h, params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert "should-not-appear" not in client.get(AUDIT, headers=h).text  # redacted when written


def test_audit_viewer_needs_audit_read_and_validates_paging(db, client):
    assert client.get(AUDIT, headers=auth_headers(make_admin(db, ["finance"]))).status_code == 403
    boss = make_admin(db)
    for params in ({"limit": 0}, {"limit": 500}, {"offset": -1}, {"date_from": "yesterday"}, {"admin_user_id": "x"}):
        assert client.get(AUDIT, headers=auth_headers(boss), params=params).status_code == 422, params
