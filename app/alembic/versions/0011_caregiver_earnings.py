"""Caregiver earnings: credited on completion, held, then released to the withdrawable balance.

Revision ID: 0011_caregiver_earnings
Revises: 0010_dropout_fee_tiers
Create Date: 2026-10-07

- ``booking_policies``: the admin-editable earning terms (``caregiver_visit_share_percent`` 80, ``earning_hold_hours`` 24)
  and the settings the payout and refund slices will read (``payout_cadence``, ``payout_min_naira``,
  ``finance_refund_limit_naira``). The existing row is updated by the column defaults.
- ``bookings.earnings_terms``: the terms above copied onto a booking when it is created.
- ``caregiver_transactions``: ``booking_id``, the share base and percent, ``available_at`` / ``released_at``, who last
  held or voided the row and why; one earning and one compensation per booking (unique ``booking_id, type``); a check on
  ``type`` and ``status``; a partial index for the release job.

Downgrade removes the new rows (earnings and compensations; withdrawals are kept) and everything above.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0011_caregiver_earnings'
down_revision: Union[str, None] = '0010_dropout_fee_tiers'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('booking_policies', sa.Column('caregiver_visit_share_percent', sa.Integer(), nullable=False, server_default='80'))
    op.add_column('booking_policies', sa.Column('earning_hold_hours', sa.Integer(), nullable=False, server_default='24'))
    op.add_column('booking_policies', sa.Column('payout_cadence', sa.String(), nullable=False, server_default='weekly'))
    op.add_column('booking_policies', sa.Column('payout_min_naira', sa.Integer(), nullable=False, server_default='5000'))
    op.add_column('booking_policies', sa.Column('finance_refund_limit_naira', sa.Integer(), nullable=False, server_default='50000'))
    for column in ('caregiver_visit_share_percent', 'earning_hold_hours', 'payout_cadence', 'payout_min_naira',
                   'finance_refund_limit_naira'):
        op.alter_column('booking_policies', column, server_default=None)  # the model carries the defaults
    op.create_check_constraint('ck_booking_policies_visit_share_percent', 'booking_policies',
                               'caregiver_visit_share_percent BETWEEN 0 AND 100')
    op.create_check_constraint('ck_booking_policies_earning_hold_hours', 'booking_policies',
                               'earning_hold_hours BETWEEN 0 AND 168')
    op.create_check_constraint('ck_booking_policies_payout_cadence', 'booking_policies',
                               "payout_cadence IN ('weekly','biweekly','monthly')")
    op.create_check_constraint('ck_booking_policies_naira', 'booking_policies',
                               'payout_min_naira BETWEEN 0 AND 100000000 AND finance_refund_limit_naira BETWEEN 0 AND 100000000')

    op.add_column('bookings', sa.Column('earnings_terms', sa.JSON(), nullable=True))

    op.add_column('caregiver_transactions', sa.Column('booking_id', sa.UUID(), nullable=True))
    op.add_column('caregiver_transactions', sa.Column('base_amount_kobo', sa.BigInteger(), nullable=True))
    op.add_column('caregiver_transactions', sa.Column('share_percent', sa.Integer(), nullable=True))
    op.add_column('caregiver_transactions', sa.Column('available_at', sa.DateTime(), nullable=True))
    op.add_column('caregiver_transactions', sa.Column('released_at', sa.DateTime(), nullable=True))
    op.add_column('caregiver_transactions', sa.Column('status_reason', sa.Text(), nullable=True))
    op.add_column('caregiver_transactions', sa.Column('status_changed_by', sa.UUID(), nullable=True))
    op.add_column('caregiver_transactions', sa.Column('status_changed_at', sa.DateTime(), nullable=True))
    op.create_foreign_key('fk_caregiver_transactions_booking', 'caregiver_transactions', 'bookings', ['booking_id'], ['id'])
    op.create_foreign_key('fk_caregiver_transactions_changed_by', 'caregiver_transactions', 'admin_users',
                          ['status_changed_by'], ['id'])
    op.create_unique_constraint('uq_caregiver_transactions_booking_type', 'caregiver_transactions', ['booking_id', 'type'])
    op.create_check_constraint('ck_caregiver_transactions_type', 'caregiver_transactions',
                               "type IN ('earning','late_cancel_compensation','withdrawal')")
    op.create_check_constraint('ck_caregiver_transactions_status', 'caregiver_transactions',
                               "status IN ('pending','successful','failed','pending_clearance','held','voided')")
    op.create_index('ix_caregiver_transactions_release', 'caregiver_transactions', ['available_at'],
                    postgresql_where=sa.text("status = 'pending_clearance'"))
    op.create_index('ix_caregiver_transactions_caregiver', 'caregiver_transactions', ['caregiver_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_caregiver_transactions_caregiver', table_name='caregiver_transactions')
    op.drop_index('ix_caregiver_transactions_release', table_name='caregiver_transactions')
    op.drop_constraint('ck_caregiver_transactions_status', 'caregiver_transactions', type_='check')
    op.drop_constraint('ck_caregiver_transactions_type', 'caregiver_transactions', type_='check')
    # Rows that exist only because of this feature; a balance already released from them stays in the wallet.
    op.execute("DELETE FROM caregiver_transactions WHERE type IN ('earning', 'late_cancel_compensation') "
               "AND booking_id IS NOT NULL")
    op.drop_constraint('uq_caregiver_transactions_booking_type', 'caregiver_transactions', type_='unique')
    op.drop_constraint('fk_caregiver_transactions_changed_by', 'caregiver_transactions', type_='foreignkey')
    op.drop_constraint('fk_caregiver_transactions_booking', 'caregiver_transactions', type_='foreignkey')
    for column in ('status_changed_at', 'status_changed_by', 'status_reason', 'released_at', 'available_at',
                   'share_percent', 'base_amount_kobo', 'booking_id'):
        op.drop_column('caregiver_transactions', column)
    op.drop_column('bookings', 'earnings_terms')
    for name in ('ck_booking_policies_naira', 'ck_booking_policies_payout_cadence',
                 'ck_booking_policies_earning_hold_hours', 'ck_booking_policies_visit_share_percent'):
        op.drop_constraint(name, 'booking_policies', type_='check')
    for column in ('finance_refund_limit_naira', 'payout_min_naira', 'payout_cadence', 'earning_hold_hours',
                   'caregiver_visit_share_percent'):
        op.drop_column('booking_policies', column)
