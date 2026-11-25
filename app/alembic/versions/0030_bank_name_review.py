"""Bank account holder-name review: ``caregiver_bank_accounts.name_review_status`` and ``name_review_at``.

Revision ID: 0030_bank_name_review
Revises: 0029_location_nudged_at
Create Date: 2026-11-25

A payout account whose holder name does not look like the caregiver's profile name used to be refused. It is now saved and held:
``name_review_status`` is ``pending`` until Finance approves or rejects it (``approved`` / ``rejected``); NULL means the name
matched and there is nothing to review. Payouts skip an account that is pending or rejected. Both columns are NULL on every
existing row, so no existing account is held.

Downgrade drops both columns (a held account is then paid like any other, so decide the open reviews first).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0030_bank_name_review'
down_revision: Union[str, None] = '0029_location_nudged_at'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('caregiver_bank_accounts', sa.Column('name_review_status', sa.String(), nullable=True))
    op.add_column('caregiver_bank_accounts', sa.Column('name_review_at', sa.DateTime(), nullable=True))
    op.create_check_constraint('ck_bank_name_review_status', 'caregiver_bank_accounts',
                               "name_review_status IS NULL OR name_review_status IN ('pending', 'approved', 'rejected')")


def downgrade() -> None:
    op.drop_constraint('ck_bank_name_review_status', 'caregiver_bank_accounts', type_='check')
    op.drop_column('caregiver_bank_accounts', 'name_review_at')
    op.drop_column('caregiver_bank_accounts', 'name_review_status')
