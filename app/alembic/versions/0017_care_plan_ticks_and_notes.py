"""Care plans, ticks and notes: task completions and observations.

Revision ID: 0017_care_plan_ticks_and_notes
Revises: 0016_care_plans_core
Create Date: 2026-10-28

The design doc (docs/plans/2026-10-27-caregiver-care-plans-design.md section 14) calls this revision ``0016``; KYC took
``0015_caregiver_kyc`` and care plans B1 ``0016_care_plans_core`` first.

- ``care_plan_task_completions``: one row per ticked task per day (``UNIQUE (task_id, for_date)``, so two caregivers racing
  on the same tick get one row). Holds no health text: ids, a date and a timestamp. Unticking deletes the row and the
  service writes a ``task.untick`` event.
- ``care_plan_observations``: visit notes, append-only by convention (no edit or delete route; there is deliberately no
  database trigger, because account erasure must blank ``body_encrypted``). The body is Fernet ciphertext; the column is
  nullable only so that erasure can wipe it. ``family_visible`` defaults to false and no v1 request sets it.

Downgrade drops both tables and every tick and note in them. Do not downgrade once real visits have been recorded.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0017_care_plan_ticks_and_notes'
down_revision: Union[str, None] = '0016_care_plans_core'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'care_plan_task_completions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.UUID(), nullable=False),
        sa.Column('for_date', sa.Date(), nullable=False),
        sa.Column('completed_by_caregiver_id', sa.UUID(), nullable=False),
        sa.Column('booking_id', sa.UUID(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['care_plan_tasks.id']),
        sa.ForeignKeyConstraint(['completed_by_caregiver_id'], ['caregivers.id']),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'for_date', name='uq_care_plan_task_completions_task_date'),
    )
    op.create_index('ix_care_plan_task_completions_booking', 'care_plan_task_completions', ['booking_id'])

    op.create_table(
        'care_plan_observations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('booking_id', sa.UUID(), nullable=True),
        sa.Column('author_caregiver_id', sa.UUID(), nullable=True),
        sa.Column('author_admin_id', sa.UUID(), nullable=True),
        sa.Column('category', sa.String(), server_default=sa.text("'observation'"), nullable=False),
        sa.Column('body_encrypted', sa.Text(), nullable=True),
        sa.Column('family_visible', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['care_plans.id']),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.ForeignKeyConstraint(['author_caregiver_id'], ['caregivers.id']),
        sa.ForeignKeyConstraint(['author_admin_id'], ['admin_users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('num_nonnulls(author_caregiver_id, author_admin_id) = 1',
                           name='ck_care_plan_observations_one_author'),
        sa.CheckConstraint("category IN ('observation','special_note')", name='ck_care_plan_observations_category'),
    )
    op.create_index('ix_care_plan_observations_plan_created', 'care_plan_observations',
                    ['plan_id', sa.text('created_at DESC')])


def downgrade() -> None:
    op.drop_table('care_plan_observations')
    op.drop_table('care_plan_task_completions')
