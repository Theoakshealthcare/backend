"""Caregiver arrival, the short-visit threshold as policy, and the family's cancel reason.

Revision ID: 0024_arrival_policy_cancel_note
Revises: 0023_booking_payment_link
Create Date: 2026-11-07

* ``bookings.arrived_at``: when the assigned caregiver tapped "I've arrived" (NULL until then; cleared when the booking is
  given to someone else).
* ``bookings.cancel_note``: the free-text reason the booker gave when cancelling (up to 500 characters). A paid cancel kept
  it only on the refund's note; a free cancel of an unpaid booking kept it nowhere.
* ``booking_policies.short_visit_min_percent``: the share of the booked time a caregiver must spend on site before a
  completed visit's earning clears without a Finance look (was the code constant ``SHORT_VISIT_MIN_PERCENT = 50``). The
  existing row gets 50, so behaviour does not change. It is also copied into each new booking's ``earnings_terms``.

Downgrade drops the three columns (the stored arrival times and notes are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0024_arrival_policy_cancel_note'
down_revision: Union[str, None] = '0023_booking_payment_link'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('arrived_at', sa.DateTime(), nullable=True))
    op.add_column('bookings', sa.Column('cancel_note', sa.Text(), nullable=True))
    op.add_column('booking_policies', sa.Column('short_visit_min_percent', sa.Integer(), nullable=False,
                                                server_default='50'))
    op.create_check_constraint('ck_booking_policies_short_visit_min_percent', 'booking_policies',
                               'short_visit_min_percent BETWEEN 0 AND 100')


def downgrade() -> None:
    op.drop_constraint('ck_booking_policies_short_visit_min_percent', 'booking_policies', type_='check')
    op.drop_column('booking_policies', 'short_visit_min_percent')
    op.drop_column('bookings', 'cancel_note')
    op.drop_column('bookings', 'arrived_at')
