"""``bookings.paystack_authorization_url`` / ``paystack_access_code``: the Paystack checkout link a booking was created with.

Revision ID: 0023_booking_payment_link
Revises: 0022_password_reset_code
Create Date: 2026-11-07

``POST /booking/{id}/payment-link`` used to ask Paystack to initialise the booking's reference a second time. Paystack
refuses a reference it has already seen, so against the real Paystack that answered 503. The link ``/create`` (or the
WhatsApp booking flow) got is now kept on the booking and handed back.

Both columns are nullable: bookings created before this migration have no stored link (their payment window is
``PAYMENT_LINK_TTL_MINUTES`` long, so they drain on their own). The columns are named ``paystack_*`` on purpose: the booking
response models already have ``payment_url`` / ``payment_access_code`` fields, and a same-named column could be copied into
a view that must not carry it.

Downgrade drops both columns; a pending booking then simply has no stored link (the route falls back as for old bookings).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0023_booking_payment_link'
down_revision: Union[str, None] = '0022_password_reset_code'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('paystack_authorization_url', sa.String(), nullable=True))
    op.add_column('bookings', sa.Column('paystack_access_code', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'paystack_access_code')
    op.drop_column('bookings', 'paystack_authorization_url')
