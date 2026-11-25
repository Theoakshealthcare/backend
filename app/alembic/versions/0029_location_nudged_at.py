"""Stale-location nudge: ``caregiver_locations.location_nudged_at`` (nullable timestamp).

Revision ID: 0029_location_nudged_at
Revises: 0028_caregiver_applying_as
Create Date: 2026-11-25

An available caregiver whose last location fix is older than AVAILABLE_LOCATION_MAX_HOURS is pushed "Update your
location" by a cron job (app/services/location_nudge.py), at most once per LOCATION_NUDGE_MIN_GAP_HOURS. This column
records when the last push went out so the job can keep to that gap. NULL on every existing row (never nudged).

Downgrade drops the column (the gap bookkeeping is lost; nothing else reads it).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0029_location_nudged_at'
down_revision: Union[str, None] = '0028_caregiver_applying_as'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('caregiver_locations', sa.Column('location_nudged_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('caregiver_locations', 'location_nudged_at')
