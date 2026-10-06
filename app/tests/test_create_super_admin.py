"""The bootstrap CLI: python -m scripts.create_super_admin."""

import pytest
from sqlalchemy import select

from app.core.security import verify_password
from app.db.models.admin import AdminUser
from app.tests.admin_helpers import LOGIN, VERIFY
from app.tests.factories import make_admin
from scripts.create_super_admin import PASSWORD_ENV, main

STRONG = "Bootstrap-Pass-2026-x"  # placeholder test password


def _rows(db):
    db.expire_all()
    return list(db.scalars(select(AdminUser)))


def test_creates_an_active_super_admin_without_mfa(db, monkeypatch, capsys):
    monkeypatch.setenv(PASSWORD_ENV, STRONG)
    assert main(["--email", "Founder@Example.com", "--name", "Founder One"]) == 0

    (admin,) = _rows(db)
    assert admin.email == "founder@example.com" and admin.full_name == "Founder One"
    assert admin.status == "active" and admin.role_keys == ["super_admin"]
    assert admin.mfa_enabled is False and admin.mfa_secret_enc is None
    assert verify_password(STRONG, admin.password_hash) and STRONG not in admin.password_hash

    out = capsys.readouterr()
    assert STRONG not in out.out + out.err


def test_the_new_admin_is_forced_through_mfa_enrolment(db, client, admin_clock, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, STRONG)
    main(["--email", "founder@example.com"])
    login = client.post(LOGIN, json={"email": "founder@example.com", "password": STRONG})
    assert login.status_code == 200 and login.json()["mfa_enrollment_required"] is True
    r = client.post(VERIFY, json={"mfa_token": login.json()["mfa_token"],
                                  "code": admin_clock.code(login.json()["secret"])})
    assert r.status_code == 200
    assert "admins.manage" in r.json()["admin"]["permissions"]


def test_prompts_when_the_variable_is_not_set(db, monkeypatch):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    answers = iter([STRONG, STRONG])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--email", "founder@example.com"]) == 0
    assert len(_rows(db)) == 1


def test_prompt_mismatch_creates_nothing(db, monkeypatch, capsys):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    answers = iter([STRONG, STRONG + "x"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--email", "founder@example.com"]) == 2
    assert _rows(db) == []
    assert "do not match" in capsys.readouterr().err


def test_no_password_source_is_an_error(db, monkeypatch, capsys):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["--email", "founder@example.com"]) == 2
    assert PASSWORD_ENV in capsys.readouterr().err
    assert _rows(db) == []


@pytest.mark.parametrize("password", ["short", "aaaaaaaaaaaaaaaaaaaa", "founder-founder-1234"])
def test_weak_passwords_are_refused(db, monkeypatch, password):
    monkeypatch.setenv(PASSWORD_ENV, password)
    assert main(["--email", "founder@example.com"]) == 2
    assert _rows(db) == []


def test_refuses_an_existing_email_and_changes_nothing(db, monkeypatch, capsys):
    existing = make_admin(db, ["support"], email="founder@example.com")
    before = existing.password_hash
    monkeypatch.setenv(PASSWORD_ENV, STRONG)
    assert main(["--email", "FOUNDER@example.com"]) == 1
    assert "already exists" in capsys.readouterr().err
    (row,) = _rows(db)
    assert row.password_hash == before and row.role_keys == ["support"]


def test_rejects_a_value_that_is_not_an_email(db, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, STRONG)
    assert main(["--email", "nobody"]) == 2
    assert _rows(db) == []
