"""add_missing_caregiver_verification_columns

Revision ID: add_missing_caregiver_verification_columns
Revises: 4da1b7440491
Create Date: 2025-01-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'add_caregiver_verification_cols'
down_revision: Union[str, None] = '4da1b7440491'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add missing columns to caregivers table
    op.add_column('caregivers', sa.Column('document_verification_result', postgresql.JSON(), nullable=True))
    op.add_column('caregivers', sa.Column('face_verification_result', postgresql.JSON(), nullable=True))
    op.add_column('caregivers', sa.Column('face_verification_status', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove the added columns
    op.drop_column('caregivers', 'face_verification_status')
    op.drop_column('caregivers', 'face_verification_result')
    op.drop_column('caregivers', 'document_verification_result') 