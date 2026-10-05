"""jobs and dispatch: unpaid expiry columns, dispatch state, booking_dispatch_waves

Revision ID: 9b1d4e7a3c52
Revises: 2e543a04ee5d
Create Date: 2026-10-05

Adds bookings.payment_expires_at / cancel_reason (unpaid expiry), bookings.dispatch_state /
dispatch_started_at (wave dispatch) and the booking_dispatch_waves table. No data is converted:
every new bookings column is nullable and existing rows keep NULLs (the expiry job falls back to
created_at + the TTL for rows without payment_expires_at; dispatch_state NULL means "paid before
dispatch waves existed"). Downgrade drops the table and the columns, losing dispatch history only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9b1d4e7a3c52"
down_revision: Union[str, None] = "2e543a04ee5d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("bookings", sa.Column("payment_expires_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("cancel_reason", sa.String(), nullable=True))
    op.add_column("bookings", sa.Column("dispatch_state", sa.String(), nullable=True))
    op.add_column("bookings", sa.Column("dispatch_started_at", sa.DateTime(), nullable=True))
    op.create_check_constraint(
        "ck_bookings_dispatch_state", "bookings",
        "dispatch_state IN ('dispatching','matched','unmatched')",
    )

    op.create_table(
        "booking_dispatch_waves",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("booking_id", UUID, sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("wave_no", sa.Integer(), nullable=False),
        sa.Column("notified_caregiver_ids", postgresql.ARRAY(UUID), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("booking_id", "wave_no", name="uq_booking_dispatch_waves_booking_wave"),
    )


def downgrade() -> None:
    op.drop_table("booking_dispatch_waves")
    op.drop_constraint("ck_bookings_dispatch_state", "bookings", type_="check")
    op.drop_column("bookings", "dispatch_started_at")
    op.drop_column("bookings", "dispatch_state")
    op.drop_column("bookings", "cancel_reason")
    op.drop_column("bookings", "payment_expires_at")
