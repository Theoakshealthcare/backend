"""0013_finance_audit_read: adds one role-permission row, idempotently, and the downgrade removes only that row."""

from alembic import command
from sqlalchemy import text

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0013_finance_audit_read", "0012_caregiver_payouts"


def _perms(eng, role):
    with eng.connect() as conn:
        return {p for (p,) in conn.execute(text(
            "SELECT p.permission FROM admin_role_permissions p JOIN admin_roles r ON r.id = p.role_id WHERE r.key = :k"),
            {"k": role})}


def test_finance_gains_audit_read_and_downgrade_removes_only_that(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    before = _perms(scratch, "finance")
    assert "audit.read" not in before and "payouts.execute" in before

    _run(scratch, command.upgrade, THIS)
    assert _perms(scratch, "finance") == before | {"audit.read"}
    assert "audit.read" not in _perms(scratch, "operations") | _perms(scratch, "support")
    assert "audit.read" in _perms(scratch, "super_admin")

    _run(scratch, command.downgrade, PREVIOUS)
    assert _perms(scratch, "finance") == before
    assert "audit.read" in _perms(scratch, "super_admin")  # the Super Admin keeps it

    _run(scratch, command.upgrade, THIS)  # a round trip works
    assert "audit.read" in _perms(scratch, "finance")
