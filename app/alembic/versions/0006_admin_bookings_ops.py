"""Admin bookings operations (A2): the no-show flag.

Revision ID: 0006_admin_bookings_ops
Revises: 0005_whatsapp_booking
Create Date: 2026-10-06

- ``bookings.no_show_flagged_at`` / ``no_show_flagged_by``: set when an admin flags the assigned caregiver as a
  no-show (AD-FR-26); cleared when the booking is reassigned. Everything else A2 does reuses existing columns.

Downgrade drops both columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0006_admin_bookings_ops'
down_revision: Union[str, None] = '0005_whatsapp_booking'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('no_show_flagged_at', sa.DateTime(), nullable=True))
    op.add_column('bookings', sa.Column('no_show_flagged_by', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_bookings_no_show_flagged_by', 'bookings', 'admin_users', ['no_show_flagged_by'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_bookings_no_show_flagged_by', 'bookings', type_='foreignkey')
    op.drop_column('bookings', 'no_show_flagged_by')
    op.drop_column('bookings', 'no_show_flagged_at')
