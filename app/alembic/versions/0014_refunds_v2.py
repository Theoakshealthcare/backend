"""Refunds v2: refund tables, partially refunded status, caregiver owed balance, refund take-back ledger type.

Revision ID: 0014_refunds_v2
Revises: 0013_finance_audit_read
Create Date: 2026-10-21

- ``refunds`` / ``refund_attempts``: one refund per request (many per booking over time), one attempt per call to Paystack. A
  partial unique index allows one open refund per booking (``failed`` is open: it can still be retried).
- ``paymentstatus``: the ``PARTIALLY_REFUNDED`` value (the enum stores member names, as in 0001).
- ``bookings.paystack_fee_kobo``: Paystack's fee, recorded at settlement (null for older bookings).
- ``caregiver_wallets.owed_kobo`` (``>= 0``): what a take-back could not debit; ``caregiver_transactions`` type gains
  ``refund_take_back``.
- ``uq_caregiver_transactions_booking_type`` becomes a partial unique index that skips ``refund_take_back`` (several per booking).
- ``patient_transactions.refund_id``: the refund a wallet credit came from.
- ``refunds.review_flagged_at`` / ``review_reason``: a Paystack contradiction on a refund that cannot leave its status (it is
  finished, or the booking has another open refund) is flagged here so the console queue can show it (spec 4.3).
- ``refund_attempts.paystack_refund_id`` is unique where set: one Paystack refund belongs to one attempt.
- ``booking_events``: the ``refund_needs_bank_details`` event (the family is told Paystack needs their bank account).
- ``admin_role_permissions``: ``refunds.approve_above_limit`` for the Super Admin only.

Downgrade drops the tables, columns and CHECKs and removes ``refund_take_back`` ledger rows (the type CHECK cannot be restored
while they exist). It leaves the ``PARTIALLY_REFUNDED`` enum value in place (Postgres cannot drop an enum value); a booking
holding it would need a manual fix before the old code reads it. Do not downgrade once real refunds exist; write a forward
migration instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0014_refunds_v2'
down_revision: Union[str, None] = '0013_finance_audit_read'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OPEN = "'pending_approval','approved','processing','needs_attention','needs_review','failed'"
STATUSES = OPEN + ",'succeeded','rejected','closed'"
REASONS = ("'cancelled_by_booker','caregiver_cancelled','no_caregiver_found','admin_cancelled','paid_after_expiry',"
           "'paid_after_cancellation','complaint','other'")
ATTEMPT_STATUSES = "'sending','pending','processing','needs_attention','processed','failed','unknown'"
TX_TYPES_OLD = "'earning','late_cancel_compensation','withdrawal','payout'"
TX_TYPES_NEW = TX_TYPES_OLD + ",'refund_take_back'"
PERMISSION = 'refunds.approve_above_limit'
OLD_EVENTS = (
    "payment_received", "payment_link_expiring", "booking_cancelled_unpaid", "payment_refund_pending",
    "caregiver_assigned", "visit_code_issued", "caregiver_en_route", "visit_started", "visit_completed",
    "booking_cancelled_by_caregiver", "booking_cancelled_by_admin", "refund_flagged", "refund_processed",
    "caregiver_reassigned", "caregiver_dropped_replacing", "dropout_choice_needed", "dropout_handed_over",
    "caregiver_booking_cancelled",
)
NEW_EVENTS = OLD_EVENTS + ("refund_needs_bank_details",)


def _events_check(events) -> str:
    return "event IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    # Allowed inside the migration transaction on PostgreSQL 12+ because nothing here uses the new value; an autocommit block
    # would break the migration tests, which run alembic on a connection that already has a transaction.
    op.execute("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'PARTIALLY_REFUNDED'")

    op.add_column('bookings', sa.Column('paystack_fee_kobo', sa.BigInteger(), nullable=True))
    op.add_column('caregiver_wallets', sa.Column('owed_kobo', sa.BigInteger(), server_default='0', nullable=False))
    op.create_check_constraint('ck_caregiver_wallets_owed_not_negative', 'caregiver_wallets', 'owed_kobo >= 0')
    op.drop_constraint('ck_caregiver_transactions_type', 'caregiver_transactions', type_='check')
    op.create_check_constraint('ck_caregiver_transactions_type', 'caregiver_transactions', f"type IN ({TX_TYPES_NEW})")
    # Several refund_take_back rows per booking are legitimate, so the one-per-(booking, type) rule skips that type.
    op.drop_constraint('uq_caregiver_transactions_booking_type', 'caregiver_transactions', type_='unique')
    op.create_index('uq_caregiver_transactions_booking_type', 'caregiver_transactions', ['booking_id', 'type'], unique=True,
                    postgresql_where=sa.text("type <> 'refund_take_back'"))

    op.create_table(
        'refunds',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('booking_id', sa.UUID(), nullable=False),
        sa.Column('patient_id', sa.UUID(), nullable=False),
        sa.Column('reason_code', sa.String(), nullable=False),
        sa.Column('origin', sa.String(), nullable=False),
        sa.Column('requested_by_admin_id', sa.UUID(), nullable=True),
        sa.Column('suggested_amount_kobo', sa.BigInteger(), nullable=True),
        sa.Column('amount_kobo', sa.BigInteger(), nullable=True),
        sa.Column('destination', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('approved_by_admin_id', sa.UUID(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('rejected_reason', sa.Text(), nullable=True),
        sa.Column('take_back_from_caregiver', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('take_back_kobo', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('take_back_reason', sa.Text(), nullable=True),
        sa.Column('take_back_debited_kobo', sa.BigInteger(), nullable=True),
        sa.Column('take_back_owed_kobo', sa.BigInteger(), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('review_flagged_at', sa.DateTime(), nullable=True),
        sa.Column('review_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id']),
        sa.ForeignKeyConstraint(['requested_by_admin_id'], ['admin_users.id']),
        sa.ForeignKeyConstraint(['approved_by_admin_id'], ['admin_users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('amount_kobo IS NULL OR amount_kobo > 0', name='ck_refunds_amount_positive'),
        sa.CheckConstraint('suggested_amount_kobo IS NULL OR suggested_amount_kobo >= 0', name='ck_refunds_suggested_not_negative'),
        sa.CheckConstraint('take_back_kobo >= 0', name='ck_refunds_take_back_not_negative'),
        sa.CheckConstraint('take_back_debited_kobo >= 0 AND take_back_owed_kobo >= 0', name='ck_refunds_take_back_split_not_negative'),
        sa.CheckConstraint(f"status IN ({STATUSES})", name='ck_refunds_status'),
        sa.CheckConstraint(f"reason_code IN ({REASONS})", name='ck_refunds_reason_code'),
        sa.CheckConstraint("origin IN ('system','staff')", name='ck_refunds_origin'),
        sa.CheckConstraint("destination IS NULL OR destination IN ('original_method','wallet')", name='ck_refunds_destination'),
    )
    op.create_index('uq_refunds_one_open_per_booking', 'refunds', ['booking_id'], unique=True,
                    postgresql_where=sa.text(f"status IN ({OPEN})"))
    op.create_index('ix_refunds_status', 'refunds', ['status', 'created_at'])
    op.create_index('ix_refunds_booking', 'refunds', ['booking_id'])

    op.create_table(
        'refund_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('refund_id', sa.UUID(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('paystack_refund_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('bank_details_submitted_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['refund_id'], ['refunds.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(f"status IN ({ATTEMPT_STATUSES})", name='ck_refund_attempts_status'),
        sa.UniqueConstraint('refund_id', 'attempt', name='uq_refund_attempts_refund_attempt'),
    )
    op.create_index('uq_refund_attempts_paystack_refund_id', 'refund_attempts', ['paystack_refund_id'], unique=True,
                    postgresql_where=sa.text('paystack_refund_id IS NOT NULL'))

    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _events_check(NEW_EVENTS))

    op.add_column('patient_transactions', sa.Column('refund_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_patient_transactions_refund_id', 'patient_transactions', 'refunds', ['refund_id'], ['id'])

    op.execute(
        f"INSERT INTO admin_role_permissions (role_id, permission) "
        f"SELECT id, '{PERMISSION}' FROM admin_roles WHERE key = 'super_admin' ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.execute("DELETE FROM booking_events WHERE event = 'refund_needs_bank_details'")
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _events_check(OLD_EVENTS))
    op.execute(f"DELETE FROM admin_role_permissions WHERE permission = '{PERMISSION}'")
    op.drop_constraint('fk_patient_transactions_refund_id', 'patient_transactions', type_='foreignkey')
    op.drop_column('patient_transactions', 'refund_id')
    op.drop_table('refund_attempts')
    op.drop_table('refunds')
    op.execute("DELETE FROM caregiver_transactions WHERE type = 'refund_take_back'")
    op.drop_index('uq_caregiver_transactions_booking_type', table_name='caregiver_transactions')
    op.create_unique_constraint('uq_caregiver_transactions_booking_type', 'caregiver_transactions', ['booking_id', 'type'])
    op.drop_constraint('ck_caregiver_transactions_type', 'caregiver_transactions', type_='check')
    op.create_check_constraint('ck_caregiver_transactions_type', 'caregiver_transactions', f"type IN ({TX_TYPES_OLD})")
    op.drop_constraint('ck_caregiver_wallets_owed_not_negative', 'caregiver_wallets', type_='check')
    op.drop_column('caregiver_wallets', 'owed_kobo')
    op.drop_column('bookings', 'paystack_fee_kobo')
