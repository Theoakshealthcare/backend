"""Add payment_failure_reason to bookings table

Revision ID: f1a2b3c4d5e6
Revises: e44e826b4ea4
Create Date: 2025-09-12 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'e44e826b4ea4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add payment_failure_reason column to bookings table
    op.add_column('bookings', sa.Column('payment_failure_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    # Remove payment_failure_reason column from bookings table
    op.drop_column('bookings', 'payment_failure_reason')