"""WhatsApp notifications and cancellation (W3): more outbox events, a dedupe key, and the refund amount.

Revision ID: 0006_whatsapp_notifications
Revises: 0005_whatsapp_booking
Create Date: 2026-10-06

- ``booking_events``: ``dedupe_key`` (so an event that can recur for one booking, e.g. a replacement caregiver, is
  unique per key rather than per booking), the unique constraint now covers ``(booking_id, event, dedupe_key)``,
  and the event check accepts the W3 events (the visit's journey, cancellations, refunds, the caregiver push).
- ``bookings.refund_amount_kobo``: what to refund when the cancellation policy keeps a fee (NULL = the refund
  module decides, as before).

Downgrade removes the W3 events (they are notifications nobody can still be waiting for), the key and the amount.
Renumber ``down_revision`` if another migration lands first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0006_whatsapp_notifications'
down_revision: Union[str, None] = '0005_whatsapp_booking'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_EVENTS = ("payment_received", "payment_link_expiring", "booking_cancelled_unpaid", "payment_refund_pending")
NEW_EVENTS = OLD_EVENTS + (
    "caregiver_assigned", "visit_code_issued", "caregiver_en_route", "visit_started", "visit_completed",
    "booking_cancelled_by_caregiver", "booking_cancelled_by_admin", "refund_flagged", "refund_processed",
    "caregiver_booking_cancelled",
)


def _check(events: Sequence[str]) -> str:
    return "event IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.add_column('booking_events', sa.Column('dedupe_key', sa.String(), server_default='', nullable=False))
    op.drop_constraint('uq_booking_events_booking_event', 'booking_events', type_='unique')
    op.create_unique_constraint('uq_booking_events_booking_event', 'booking_events',
                                ['booking_id', 'event', 'dedupe_key'])
    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _check(NEW_EVENTS))
    op.add_column('bookings', sa.Column('refund_amount_kobo', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'refund_amount_kobo')
    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.get_bind().execute(
        sa.text("DELETE FROM booking_events WHERE event NOT IN :old").bindparams(
            sa.bindparam("old", value=tuple(OLD_EVENTS), expanding=True)))
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _check(OLD_EVENTS))
    op.drop_constraint('uq_booking_events_booking_event', 'booking_events', type_='unique')
    # A booking may hold the same event under two keys now; keep the earliest so the narrower key can be restored.
    op.execute("DELETE FROM booking_events a USING booking_events b "
               "WHERE a.booking_id = b.booking_id AND a.event = b.event AND a.created_at > b.created_at")
    op.execute("DELETE FROM booking_events a USING booking_events b "
               "WHERE a.booking_id = b.booking_id AND a.event = b.event AND a.created_at = b.created_at AND a.id > b.id")
    op.create_unique_constraint('uq_booking_events_booking_event', 'booking_events', ['booking_id', 'event'])
    op.drop_column('booking_events', 'dedupe_key')
