"""Caregiver payouts: scheduled Paystack bank transfers, approved by an admin.

Revision ID: 0012_caregiver_payouts
Revises: 0011_caregiver_earnings
Create Date: 2026-10-12

- ``payout_batches`` / ``payouts``: one batch per payout day, one payout per caregiver in it, each pointing at the
  ``caregiver_transactions`` row (type ``payout``) that holds the reserved money. A unique partial index allows one live
  payout (pending_approval or processing) per caregiver; ``needs_attention`` blocks new batches through the freeze instead.
- ``payout_attempts``: one row per send of a payout, with its own unique Paystack reference (a retry adds a row).
- ``caregiver_transactions``: the ``payout`` type, and the ``pending_approval`` and ``cancelled`` statuses.
- ``caregiver_bank_accounts``: ``bank_code``, ``recipient_code`` (Paystack), ``verified_at`` and ``payout_blocked_until``
  (the 24-hour cool-down after a change of account). Existing rows have no recipient, so they are not paid until the
  caregiver saves the account again through the verified flow.
- ``caregiver_bank_events``: history of bank account changes by the caregiver.
- ``caregiver_wallets``: a check that the balance is never negative (a second line of defence against a double spend), and
  ``payouts_frozen_at`` / ``payouts_frozen_reason`` (the caregiver-level freeze set when Paystack contradicts a payout).

Downgrade removes the new tables and the payout ledger rows. Money already paid out by Paystack is not undone by it, and a
payout reserved at the time would lose its debit (the money would look available again), so do not downgrade past this
revision once real payouts exist; reconcile by hand or write a forward migration instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0012_caregiver_payouts'
down_revision: Union[str, None] = '0011_caregiver_earnings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ck_caregiver_transactions_type', 'caregiver_transactions', type_='check')
    op.drop_constraint('ck_caregiver_transactions_status', 'caregiver_transactions', type_='check')
    op.create_check_constraint('ck_caregiver_transactions_type', 'caregiver_transactions',
                               "type IN ('earning','late_cancel_compensation','withdrawal','payout')")
    op.create_check_constraint('ck_caregiver_transactions_status', 'caregiver_transactions',
                               "status IN ('pending','successful','failed','pending_clearance','held','voided',"
                               "'pending_approval','cancelled')")
    op.create_check_constraint('ck_caregiver_wallets_balance_not_negative', 'caregiver_wallets', 'balance >= 0')
    op.add_column('caregiver_wallets', sa.Column('payouts_frozen_at', sa.DateTime(), nullable=True))
    op.add_column('caregiver_wallets', sa.Column('payouts_frozen_reason', sa.Text(), nullable=True))

    op.add_column('caregiver_bank_accounts', sa.Column('bank_code', sa.String(), nullable=True))
    op.add_column('caregiver_bank_accounts', sa.Column('recipient_code', sa.String(), nullable=True))
    op.add_column('caregiver_bank_accounts', sa.Column('verified_at', sa.DateTime(), nullable=True))
    op.add_column('caregiver_bank_accounts', sa.Column('payout_blocked_until', sa.DateTime(), nullable=True))

    op.create_table(
        'caregiver_bank_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('caregiver_id', sa.UUID(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('before', postgresql.JSONB(), nullable=True),
        sa.Column('after', postgresql.JSONB(), nullable=True),
        sa.Column('payout_blocked_until', sa.DateTime(), nullable=True),
        sa.Column('ip', sa.String(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_caregiver_bank_events_caregiver', 'caregiver_bank_events', ['caregiver_id', 'created_at'])

    op.create_table(
        'payout_batches',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('period_key', sa.String(), nullable=False),
        sa.Column('cadence', sa.String(), nullable=False),
        sa.Column('min_payout_kobo', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('approved_by', sa.UUID(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('approval_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('pending_approval','approved','cancelled')", name='ck_payout_batches_status'),
        sa.ForeignKeyConstraint(['approved_by'], ['admin_users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('period_key'),
    )
    op.create_table(
        'payouts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('batch_id', sa.UUID(), nullable=False),
        sa.Column('caregiver_id', sa.UUID(), nullable=False),
        sa.Column('transaction_id', sa.UUID(), nullable=False),
        sa.Column('amount_kobo', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('bank_name', sa.String(), nullable=False),
        sa.Column('bank_code', sa.String(), nullable=False),
        sa.Column('account_number', sa.String(), nullable=False),
        sa.Column('account_name', sa.String(), nullable=False),
        sa.Column('recipient_code', sa.String(), nullable=False),
        sa.Column('reference', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('shortfall_kobo', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('returned_kobo', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('netted_kobo', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('transfer_code', sa.String(), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('initiated_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('approved_by', sa.UUID(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by', sa.UUID(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('amount_kobo > 0', name='ck_payouts_amount_positive'),
        sa.CheckConstraint('shortfall_kobo >= 0', name='ck_payouts_shortfall_not_negative'),
        sa.CheckConstraint('returned_kobo >= 0 AND netted_kobo >= 0', name='ck_payouts_resolution_not_negative'),
        sa.CheckConstraint("status IN ('pending_approval','processing','success','failed','reversed','cancelled',"
                           "'needs_attention')", name='ck_payouts_status'),
        sa.ForeignKeyConstraint(['batch_id'], ['payout_batches.id']),
        sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id']),
        sa.ForeignKeyConstraint(['transaction_id'], ['caregiver_transactions.id']),
        sa.ForeignKeyConstraint(['approved_by'], ['admin_users.id']),
        sa.ForeignKeyConstraint(['cancelled_by'], ['admin_users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('transaction_id'),
        sa.UniqueConstraint('reference'),
    )
    op.create_index('uq_payouts_one_active_per_caregiver', 'payouts', ['caregiver_id'], unique=True,
                    postgresql_where=sa.text("status IN ('pending_approval','processing')"))
    op.create_index('ix_payouts_batch', 'payouts', ['batch_id'])
    op.create_index('ix_payouts_status', 'payouts', ['status', 'created_at'])

    op.create_table(
        'payout_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('payout_id', sa.UUID(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('reference', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('forced', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('pending','success','failed','reversed','refused')",
                           name='ck_payout_attempts_status'),
        sa.ForeignKeyConstraint(['payout_id'], ['payouts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('reference'),
        sa.UniqueConstraint('payout_id', 'attempt', name='uq_payout_attempts_payout_attempt'),
    )
    op.create_index('ix_payout_attempts_payout_id', 'payout_attempts', ['payout_id'])


def downgrade() -> None:
    op.drop_index('ix_payout_attempts_payout_id', table_name='payout_attempts')
    op.drop_table('payout_attempts')
    op.drop_index('ix_payouts_status', table_name='payouts')
    op.drop_index('ix_payouts_batch', table_name='payouts')
    op.drop_index('uq_payouts_one_active_per_caregiver', table_name='payouts')
    op.drop_table('payouts')
    op.drop_table('payout_batches')
    op.drop_index('ix_caregiver_bank_events_caregiver', table_name='caregiver_bank_events')
    op.drop_table('caregiver_bank_events')
    for column in ('payout_blocked_until', 'verified_at', 'recipient_code', 'bank_code'):
        op.drop_column('caregiver_bank_accounts', column)
    op.drop_column('caregiver_wallets', 'payouts_frozen_reason')
    op.drop_column('caregiver_wallets', 'payouts_frozen_at')
    op.drop_constraint('ck_caregiver_wallets_balance_not_negative', 'caregiver_wallets', type_='check')
    # Payout ledger rows exist only because of this feature; money already paid out stays paid.
    op.execute("DELETE FROM caregiver_transactions WHERE type = 'payout'")
    op.drop_constraint('ck_caregiver_transactions_status', 'caregiver_transactions', type_='check')
    op.drop_constraint('ck_caregiver_transactions_type', 'caregiver_transactions', type_='check')
    op.create_check_constraint('ck_caregiver_transactions_type', 'caregiver_transactions',
                               "type IN ('earning','late_cancel_compensation','withdrawal')")
    op.create_check_constraint('ck_caregiver_transactions_status', 'caregiver_transactions',
                               "status IN ('pending','successful','failed','pending_clearance','held','voided')")
