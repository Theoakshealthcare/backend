"""Booking reschedule: ``bookings.reschedule_count``, the ``booking_reschedules`` history, and three policy settings.

Revision ID: 0034_booking_reschedule
Revises: 0030_bank_name_review
Create Date: 2026-11-25

Phase 2 (sign-off pack, part E). The number 0034 is a placeholder in a chain several open PRs extend: the lead re-points
``down_revision`` at merge time (the open PRs hold 0031, 0033 and maybe 0035).

* ``bookings.reschedule_count``: how many free reschedules the booker has used (0 on every existing booking).
* ``booking_reschedules``: one row per applied reschedule (old and new time, the price quote at the new time, the difference
  and how it is handled, the caregiver who was released, the client's ``request_id``). ``UNIQUE (booking_id, request_id)``
  where the id is given makes a repeated request a replay.
* ``booking_policies``: ``reschedule_free_hours`` (12), ``reschedule_max_free`` (2), ``reschedule_max_days_ahead`` (28), the
  admin-editable settings the sign-off pack asks for. Existing rows get the defaults.

Downgrade drops all three (the history of reschedules is lost; the bookings keep their new times).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0034_booking_reschedule'
down_revision: Union[str, None] = '0030_bank_name_review'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('reschedule_count', sa.Integer(), server_default='0', nullable=False))
    op.create_check_constraint('ck_bookings_reschedule_count', 'bookings', 'reschedule_count >= 0')

    op.add_column('booking_policies', sa.Column('reschedule_free_hours', sa.Integer(), server_default='12', nullable=False))
    op.add_column('booking_policies', sa.Column('reschedule_max_free', sa.Integer(), server_default='2', nullable=False))
    op.add_column('booking_policies',
                  sa.Column('reschedule_max_days_ahead', sa.Integer(), server_default='28', nullable=False))
    op.create_check_constraint('ck_booking_policies_reschedule_hours', 'booking_policies',
                               'reschedule_free_hours BETWEEN 0 AND 168')
    op.create_check_constraint('ck_booking_policies_reschedule_max_free', 'booking_policies',
                               'reschedule_max_free BETWEEN 0 AND 20')
    op.create_check_constraint('ck_booking_policies_reschedule_days_ahead', 'booking_policies',
                               'reschedule_max_days_ahead BETWEEN 1 AND 365')

    op.create_table(
        'booking_reschedules',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('booking_id', sa.UUID(), nullable=False),
        sa.Column('requested_by_user_id', sa.UUID(), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('old_scheduled_date', sa.DateTime(), nullable=False),
        sa.Column('new_scheduled_date', sa.DateTime(), nullable=False),
        sa.Column('old_total_kobo', sa.BigInteger(), nullable=False),
        sa.Column('new_total_kobo', sa.BigInteger(), nullable=False),
        sa.Column('price_difference_kobo', sa.BigInteger(), nullable=False),
        sa.Column('price_handling', sa.String(), nullable=False),
        sa.Column('released_caregiver_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['released_caregiver_id'], ['caregivers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("price_handling IN ('none','credit_due')", name='ck_booking_reschedules_price_handling'),
    )
    op.create_index('ix_booking_reschedules_booking', 'booking_reschedules', ['booking_id', 'created_at'])
    op.create_index('uq_booking_reschedules_request', 'booking_reschedules', ['booking_id', 'request_id'], unique=True,
                    postgresql_where=sa.text('request_id IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('uq_booking_reschedules_request', table_name='booking_reschedules')
    op.drop_index('ix_booking_reschedules_booking', table_name='booking_reschedules')
    op.drop_table('booking_reschedules')
    for name in ('ck_booking_policies_reschedule_days_ahead', 'ck_booking_policies_reschedule_max_free',
                 'ck_booking_policies_reschedule_hours'):
        op.drop_constraint(name, 'booking_policies', type_='check')
    for column in ('reschedule_max_days_ahead', 'reschedule_max_free', 'reschedule_free_hours'):
        op.drop_column('booking_policies', column)
    op.drop_constraint('ck_bookings_reschedule_count', 'bookings', type_='check')
    op.drop_column('bookings', 'reschedule_count')
