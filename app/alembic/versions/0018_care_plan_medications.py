"""Care plans, medications: the medicines a nurse records on a plan (record only).

Revision ID: 0018_care_plan_medications
Revises: 0017_care_plan_ticks_and_notes
Create Date: 2026-10-28

The design doc (docs/plans/2026-10-27-caregiver-care-plans-design.md section 14) calls this revision ``0017``; KYC took
``0015_caregiver_kyc``, care plans B1 ``0016_care_plans_core`` and B2 ``0017_care_plan_ticks_and_notes`` first.

``care_plan_medications``: one row per medicine on a plan. Name, strength, dose, route, reason, prescriber and note are one
Fernet-encrypted JSON document in ``detail_encrypted`` (nullable only so that account erasure can wipe it; the service
refuses to write a medication without it). The frequency, times of day and dates stay plaintext. A medication is stopped
(``status='stopped'`` with ``stopped_at``), never deleted. Record only: nothing here drives an alert or a reminder.

Downgrade drops the table and every medication in it. Do not downgrade once real plans carry medications.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0018_care_plan_medications'
down_revision: Union[str, None] = '0017_care_plan_ticks_and_notes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'care_plan_medications',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('detail_encrypted', sa.Text(), nullable=True),
        sa.Column('frequency', sa.String(), nullable=False),
        sa.Column('times_of_day', postgresql.ARRAY(sa.String()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column('stopped_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['care_plans.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "frequency IN ('once_daily','twice_daily','three_times_daily','four_times_daily','weekly','as_needed','other')",
            name='ck_care_plan_medications_frequency'),
        sa.CheckConstraint("status IN ('active','stopped')", name='ck_care_plan_medications_status'),
        sa.CheckConstraint('end_date IS NULL OR end_date >= start_date', name='ck_care_plan_medications_dates'),
        sa.CheckConstraint("(status = 'stopped') = (stopped_at IS NOT NULL)", name='ck_care_plan_medications_stopped'),
        sa.CheckConstraint('cardinality(times_of_day) <= 12', name='ck_care_plan_medications_times'),
    )
    op.create_index('ix_care_plan_medications_plan_status', 'care_plan_medications', ['plan_id', 'status'])


def downgrade() -> None:
    op.drop_table('care_plan_medications')
