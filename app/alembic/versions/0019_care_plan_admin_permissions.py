"""Care plan admin permissions: ``careplans.read``, ``careplans.write``, ``careplans.templates``.

Revision ID: 0019_care_plan_admin_permissions
Revises: 0018_care_plan_medications
Create Date: 2026-10-28

The design doc (docs/plans/2026-10-27-caregiver-care-plans-design.md section 14) calls this revision ``0018``; care plans B3
took that number first.

Data only: rows in ``admin_role_permissions``. The Clinical Reviewer gets all three (it authors plans and signs off templates
through the admin API; there are no console screens yet) and the Super Admin gets all three, as it holds every permission.
Nobody else. Idempotent both ways. Downgrade removes only these rows, from these two roles.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0019_care_plan_admin_permissions'
down_revision: Union[str, None] = '0018_care_plan_medications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSIONS = ('careplans.read', 'careplans.write', 'careplans.templates')
ROLES = ('super_admin', 'clinical_reviewer')


def upgrade() -> None:
    for permission in PERMISSIONS:
        for role in ROLES:
            op.execute(sa.text(
                "INSERT INTO admin_role_permissions (role_id, permission) "
                "SELECT id, :p FROM admin_roles WHERE key = :r ON CONFLICT DO NOTHING"
            ).bindparams(p=permission, r=role))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM admin_role_permissions WHERE permission IN :perms "
        "AND role_id IN (SELECT id FROM admin_roles WHERE key IN :roles)"
    ).bindparams(sa.bindparam('perms', value=PERMISSIONS, expanding=True),
                 sa.bindparam('roles', value=ROLES, expanding=True)))
