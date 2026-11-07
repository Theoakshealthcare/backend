"""``users.password_reset_code_hash`` / ``password_reset_code_expires_at``: the short emailed password-reset code.

Revision ID: 0022_password_reset_code
Revises: 0021_patient_notifications
Create Date: 2026-11-07

The reset code (5 digits) is stored only as a keyed hash with its own expiry. The old ``password_reset_token`` columns
stay for the deprecated long link token, so both flows can run side by side while the shipped caregiver app is retired.
Wrong-attempt and request counts live in ``admin_rate_limits`` (a generic counter store), not here.

Downgrade drops both columns; any code issued meanwhile simply stops working.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0022_password_reset_code'
down_revision: Union[str, None] = '0021_patient_notifications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('password_reset_code_hash', sa.String(), nullable=True))
    op.add_column('users', sa.Column('password_reset_code_expires_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'password_reset_code_expires_at')
    op.drop_column('users', 'password_reset_code_hash')
