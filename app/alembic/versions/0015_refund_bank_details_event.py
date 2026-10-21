"""Refunds v2: the ``refund_needs_bank_details`` booking event (the family is told Paystack needs their bank account).

Revision ID: 0015_refund_bank_details
Revises: 0014_refunds_v2
Create Date: 2026-10-21

``booking_events.event`` has a CHECK listing every event the outbox may hold; this adds the new one. Downgrade deletes any
``refund_needs_bank_details`` rows (the CHECK cannot be restored while they exist); they are undelivered or already-sent
notices, not records of money.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0015_refund_bank_details'
down_revision: Union[str, None] = '0014_refunds_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_EVENTS = (
    "payment_received", "payment_link_expiring", "booking_cancelled_unpaid", "payment_refund_pending",
    "caregiver_assigned", "visit_code_issued", "caregiver_en_route", "visit_started", "visit_completed",
    "booking_cancelled_by_caregiver", "booking_cancelled_by_admin", "refund_flagged", "refund_processed",
    "caregiver_reassigned", "caregiver_dropped_replacing", "dropout_choice_needed", "dropout_handed_over",
    "caregiver_booking_cancelled",
)
NEW_EVENTS = OLD_EVENTS + ("refund_needs_bank_details",)


def _check(events: Sequence[str]) -> str:
    return "event IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _check(NEW_EVENTS))


def downgrade() -> None:
    op.drop_constraint('ck_booking_events_event', 'booking_events', type_='check')
    op.get_bind().execute(sa.text("DELETE FROM booking_events WHERE event = 'refund_needs_bank_details'"))
    op.create_check_constraint('ck_booking_events_event', 'booking_events', _check(OLD_EVENTS))
