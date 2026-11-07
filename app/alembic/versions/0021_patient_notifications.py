"""``notifications``: the in-app feed for app patients, and two new booking events.

Revision ID: 0021_patient_notifications
Revises: 0020_user_token_version
Create Date: 2026-11-07

* ``notifications``: one row per thing a patient was told in the app (and by Expo push). ``event_id`` is the outbox
  row (``booking_events``) the notice came from, unique, so a retried delivery can never write a second feed row.
* ``booking_events`` learns ``caregiver_arrived`` and ``no_caregiver_found``: booker events that exist only for app
  bookings (a WhatsApp booking never records them, see ``booking_events.emit``).

Downgrade removes the two event kinds' rows, restores the old check and drops the table (feed rows are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0021_patient_notifications'
down_revision: Union[str, None] = '0020_user_token_version'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_EVENTS = (
    "payment_received", "payment_link_expiring", "booking_cancelled_unpaid", "payment_refund_pending",
    "caregiver_assigned", "visit_code_issued", "caregiver_en_route", "visit_started", "visit_completed",
    "booking_cancelled_by_caregiver", "booking_cancelled_by_admin", "refund_flagged", "refund_processed",
    "caregiver_reassigned", "caregiver_dropped_replacing", "dropout_choice_needed", "dropout_handed_over",
    "refund_needs_bank_details", "caregiver_booking_cancelled",
)
NEW_EVENTS = OLD_EVENTS + ("caregiver_arrived", "no_caregiver_found")


def _events_check(events) -> str:
    return "event IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.create_table(
        'notifications',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('event_id', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_notifications_user_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', name='uq_notifications_event_id'),
    )
    # The feed query: one user's rows, newest first.
    op.create_index('ix_notifications_user_feed', 'notifications',
                    ['user_id', sa.text('created_at DESC'), sa.text('id DESC')])
    # The unread badge: only unread rows are indexed, and they are few.
    op.create_index('ix_notifications_user_unread', 'notifications', ['user_id'],
                    postgresql_where=sa.text('read_at IS NULL'))

    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _events_check(NEW_EVENTS))


def downgrade() -> None:
    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.execute("DELETE FROM booking_events WHERE event IN ('caregiver_arrived', 'no_caregiver_found')")
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _events_check(OLD_EVENTS))
    op.drop_index('ix_notifications_user_unread', table_name='notifications')
    op.drop_index('ix_notifications_user_feed', table_name='notifications')
    op.drop_table('notifications')
