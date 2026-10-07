"""Caregiver drop-out re-dispatch, two-tier cancellation fee, and the admin-editable booking policy.

Revision ID: 0010_dropout_fee_tiers
Revises: 0009_account_erasure
Create Date: 2026-10-07

- ``booking_policies``: one row (``id = 1``) of cancellation tiers and drop-out timings an admin edits. Seeded with the
  defaults. Replaces the ``WHATSAPP_CANCEL_*`` environment settings.
- ``bookings``: ``cancel_policy`` (the tiers copied from the policy when the quote was made), ``cancel_fee_tier`` and
  ``late_cancel_compensable`` (what a booker cancellation applied), ``on_my_way_at`` / ``on_my_way_distance_m`` (so the
  movement check can be made at cancel time), ``dropout_state`` (a replacement in progress).
- ``caregiver_dropouts``: one row per caregiver who cancels an accepted visit.
- ``booking_events``: three more events (replacement started, choice needed, handed to a person).

Downgrade removes the three events (notifications nobody can still be waiting for) and everything above.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0010_dropout_fee_tiers'
down_revision: Union[str, None] = '0009_account_erasure'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_EVENTS = (
    "payment_received", "payment_link_expiring", "booking_cancelled_unpaid", "payment_refund_pending",
    "caregiver_assigned", "visit_code_issued", "caregiver_en_route", "visit_started", "visit_completed",
    "booking_cancelled_by_caregiver", "booking_cancelled_by_admin", "refund_flagged", "refund_processed",
    "caregiver_reassigned", "caregiver_booking_cancelled",
)
NEW_EVENTS = OLD_EVENTS + ("caregiver_dropped_replacing", "dropout_choice_needed", "dropout_handed_over")
STATES = ("redispatching", "awaiting_choice", "waiting", "handed_over", "closed")


def _check(events: Sequence[str]) -> str:
    return "event IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.create_table(
        'booking_policies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cancel_free_hours', sa.Integer(), nullable=False),
        sa.Column('cancel_late_hours', sa.Integer(), nullable=False),
        sa.Column('cancel_fee_percent', sa.Integer(), nullable=False),
        sa.Column('cancel_late_fee_percent', sa.Integer(), nullable=False),
        sa.Column('dropout_redispatch_min_lead_minutes', sa.Integer(), nullable=False),
        sa.Column('dropout_decision_lead_minutes', sa.Integer(), nullable=False),
        sa.Column('onmyway_compensation_window_minutes', sa.Integer(), nullable=False),
        sa.Column('caregiver_late_fee_share_percent', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.UUID(), nullable=True),
        sa.CheckConstraint('id = 1', name='ck_booking_policies_singleton'),
        sa.CheckConstraint('cancel_fee_percent BETWEEN 0 AND 100', name='ck_booking_policies_fee_percent'),
        sa.CheckConstraint('cancel_late_fee_percent BETWEEN 0 AND 100', name='ck_booking_policies_late_fee_percent'),
        sa.CheckConstraint('caregiver_late_fee_share_percent BETWEEN 0 AND 100', name='ck_booking_policies_share_percent'),
        sa.CheckConstraint('cancel_free_hours BETWEEN 0 AND 168 AND cancel_late_hours BETWEEN 0 AND 168',
                           name='ck_booking_policies_hours'),
        sa.CheckConstraint(
            'dropout_redispatch_min_lead_minutes BETWEEN 0 AND 10080 AND dropout_decision_lead_minutes BETWEEN 0 AND 10080 '
            'AND onmyway_compensation_window_minutes BETWEEN 0 AND 10080', name='ck_booking_policies_minutes'),
        sa.CheckConstraint('cancel_fee_percent <= cancel_late_fee_percent', name='ck_booking_policies_late_fee_not_lower'),
        sa.CheckConstraint('cancel_late_hours < cancel_free_hours', name='ck_booking_policies_late_before_free'),
        sa.CheckConstraint('dropout_decision_lead_minutes < dropout_redispatch_min_lead_minutes',
                           name='ck_booking_policies_decision_before_redispatch'),
        sa.ForeignKeyConstraint(['updated_by'], ['admin_users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.execute(
        "INSERT INTO booking_policies (id, cancel_free_hours, cancel_late_hours, cancel_fee_percent, "
        "cancel_late_fee_percent, dropout_redispatch_min_lead_minutes, dropout_decision_lead_minutes, "
        "onmyway_compensation_window_minutes, caregiver_late_fee_share_percent, updated_at) "
        "VALUES (1, 12, 2, 20, 50, 120, 60, 120, 80, now() AT TIME ZONE 'utc')")

    op.add_column('bookings', sa.Column('cancel_policy', sa.JSON(), nullable=True))
    op.add_column('bookings', sa.Column('cancel_fee_tier', sa.String(), nullable=True))
    op.add_column('bookings', sa.Column('late_cancel_compensable', sa.Boolean(), nullable=True))
    op.add_column('bookings', sa.Column('on_my_way_at', sa.DateTime(), nullable=True))
    op.add_column('bookings', sa.Column('on_my_way_distance_m', sa.Integer(), nullable=True))
    op.add_column('bookings', sa.Column('dropout_state', sa.String(), nullable=True))
    op.create_check_constraint('ck_bookings_cancel_fee_tier', 'bookings',
                               "cancel_fee_tier IN ('none','standard','late')")
    op.create_check_constraint('ck_bookings_dropout_state', 'bookings',
                               "dropout_state IN (" + ", ".join(f"'{s}'" for s in STATES) + ")")
    # The worker looks for bookings with a drop-out in progress: a partial index keeps that scan tiny.
    op.create_index('ix_bookings_dropout_open', 'bookings', ['scheduled_date'],
                    postgresql_where=sa.text("dropout_state IN ('redispatching','awaiting_choice','waiting')"))

    op.create_table(
        'caregiver_dropouts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('caregiver_id', sa.UUID(), nullable=False),
        sa.Column('booking_id', sa.UUID(), nullable=False),
        sa.Column('dropped_at', sa.DateTime(), nullable=False),
        sa.Column('minutes_before_start', sa.Integer(), nullable=False),
        sa.Column('was_en_route', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id']),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_caregiver_dropouts_caregiver', 'caregiver_dropouts', ['caregiver_id', 'dropped_at'])
    op.create_index('ix_caregiver_dropouts_booking', 'caregiver_dropouts', ['booking_id'])

    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _check(NEW_EVENTS))


def downgrade() -> None:
    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.get_bind().execute(
        sa.text("DELETE FROM booking_events WHERE event NOT IN :old").bindparams(
            sa.bindparam("old", value=tuple(OLD_EVENTS), expanding=True)))
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _check(OLD_EVENTS))
    op.drop_table('caregiver_dropouts')
    op.drop_index('ix_bookings_dropout_open', table_name='bookings')
    op.drop_constraint('ck_bookings_dropout_state', 'bookings', type_='check')
    op.drop_constraint('ck_bookings_cancel_fee_tier', 'bookings', type_='check')
    for column in ('dropout_state', 'on_my_way_distance_m', 'on_my_way_at', 'late_cancel_compensable',
                   'cancel_fee_tier', 'cancel_policy'):
        op.drop_column('bookings', column)
    op.drop_table('booking_policies')
