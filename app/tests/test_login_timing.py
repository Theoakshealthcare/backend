"""An unknown login must cost one bcrypt check, the same as a wrong password (no email enumeration by timing)."""

from app.services import user as user_service
from app.services.user import UserService


def test_unknown_email_still_spends_one_password_check(db, monkeypatch):
    calls = []
    real = user_service.verify_password
    monkeypatch.setattr(user_service, "verify_password", lambda pw, h: calls.append(h) or real(pw, h))
    assert UserService(db).authenticate_user("nobody@example.com", "whatever-password") is None
    assert len(calls) == 1 and calls[0].startswith("$2b$")
