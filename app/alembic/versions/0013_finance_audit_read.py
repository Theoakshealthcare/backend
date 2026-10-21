"""Finance can read the admin audit log (``audit.read``).

Revision ID: 0013_finance_audit_read
Revises: 0012_caregiver_payouts
Create Date: 2026-10-19

Owner decision of 2026-10-19: the Finance role gets ``audit.read`` so it can see the trail of the payouts it now approves.
Data only: one row in ``admin_role_permissions``. Idempotent both ways. Downgrade removes only that row.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0013_finance_audit_read'
down_revision: Union[str, None] = '0012_caregiver_payouts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO admin_role_permissions (role_id, permission) "
        "SELECT id, 'audit.read' FROM admin_roles WHERE key = 'finance' "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM admin_role_permissions WHERE permission = 'audit.read' "
        "AND role_id IN (SELECT id FROM admin_roles WHERE key = 'finance')"
    )
