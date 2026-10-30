"""0019_care_plan_admin_permissions: three permission rows for two roles, idempotently; the downgrade removes only those."""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0019_care_plan_admin_permissions", "0018_care_plan_medications"
NEW = {"careplans.read", "careplans.write", "careplans.templates"}


def _perms(eng, role):
    with eng.connect() as conn:
        return {p for (p,) in conn.execute(text(
            "SELECT p.permission FROM admin_role_permissions p JOIN admin_roles r ON r.id = p.role_id WHERE r.key = :k"),
            {"k": role})}


def test_0019_follows_its_predecessor():
    # "Single head" is asserted for the whole history by test_migrations.py; 0019 is no longer the head once 0020 exists.
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision(THIS).down_revision == PREVIOUS


def test_the_clinical_reviewer_and_super_admin_gain_the_permissions_and_downgrade_removes_only_those(scratch):
    _run(scratch, command.upgrade, PREVIOUS)
    before = {r: _perms(scratch, r) for r in ("clinical_reviewer", "super_admin", "finance", "operations")}
    assert not NEW & set().union(*before.values())

    _run(scratch, command.upgrade, THIS)
    assert _perms(scratch, "clinical_reviewer") == before["clinical_reviewer"] | NEW
    assert _perms(scratch, "super_admin") == before["super_admin"] | NEW
    for other in ("finance", "operations", "support", "verification", "read_only_analyst"):
        assert not NEW & _perms(scratch, other), other
    assert _perms(scratch, "finance") == before["finance"]

    _run(scratch, command.downgrade, PREVIOUS)
    assert {r: _perms(scratch, r) for r in before} == before

    _run(scratch, command.upgrade, THIS)  # a round trip works, and running it twice adds nothing
    _run(scratch, command.upgrade, THIS)
    assert NEW <= _perms(scratch, "clinical_reviewer") and NEW <= _perms(scratch, "super_admin")
