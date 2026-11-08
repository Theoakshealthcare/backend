"""Booking durations in minutes: ``bookings.duration_minutes`` replaces ``duration_hours``; the catalogue moves to 30-minute steps.

Revision ID: 0027_booking_minutes
Revises: 0026_service_catalog
Create Date: 2026-11-08

Phase 2, slice 2.

* ``bookings.duration_minutes`` is added, backfilled from ``duration_hours * 60``, made NOT NULL (and ``> 0``), and
  ``duration_hours`` is dropped. The model keeps ``duration_hours`` as a computed property (minutes / 60), so existing code
  and the apps' responses still see hours.
* ``service_catalog``: every service gets a 30-minute step, and the nutritionist's shortest booking becomes 30 minutes
  (the other services keep 60). Admins can change these afterwards.

Downgrade restores ``duration_hours`` as minutes / 60 **rounded up** to a whole hour (a 90-minute booking becomes 2 hours:
the old column is an integer; no real data exists yet), drops ``duration_minutes``, and puts the catalogue back on 60-minute
steps (minimum rounded up and maximum rounded down to whole hours).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0027_booking_minutes'
down_revision: Union[str, None] = '0026_service_catalog'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('duration_minutes', sa.Integer(), nullable=True))
    op.execute("UPDATE bookings SET duration_minutes = duration_hours * 60")
    op.alter_column('bookings', 'duration_minutes', nullable=False)
    op.create_check_constraint('ck_bookings_duration_positive', 'bookings', 'duration_minutes > 0')
    op.drop_column('bookings', 'duration_hours')

    op.execute("UPDATE service_catalog SET step_minutes = 30")
    op.execute("UPDATE service_catalog SET min_minutes = 30 WHERE service_type = 'nutritionist'")


def downgrade() -> None:
    op.execute("UPDATE service_catalog SET step_minutes = 60, "
               "min_minutes = GREATEST(60, CEIL(min_minutes / 60.0)::int * 60), "
               "max_minutes = GREATEST(60, FLOOR(max_minutes / 60.0)::int * 60)")
    op.execute("UPDATE service_catalog SET max_minutes = min_minutes WHERE max_minutes < min_minutes")

    op.add_column('bookings', sa.Column('duration_hours', sa.Integer(), nullable=True))
    op.execute("UPDATE bookings SET duration_hours = GREATEST(1, CEIL(duration_minutes / 60.0)::int)")
    op.alter_column('bookings', 'duration_hours', nullable=False)
    op.drop_constraint('ck_bookings_duration_positive', 'bookings', type_='check')
    op.drop_column('bookings', 'duration_minutes')
