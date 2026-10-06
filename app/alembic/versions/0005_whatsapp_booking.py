"""WhatsApp booking in chat (W2): quotes, the booking-event outbox, and the booking columns it needs.

Revision ID: 0005_whatsapp_booking
Revises: 0004_whatsapp_foundations
Create Date: 2026-10-06

- ``bookings``: ``channel`` (existing rows are ``app``), ``booked_by_user_id``, ``care_recipient_id``,
  ``condition_encrypted`` and ``needs_address_check``.
- ``quotes``: the locked price and, while a Flow is open, the draft of the booking (one booking per quote).
- ``booking_events``: an outbox of things worth telling the person (payment received, link expiring, ...),
  unique per booking and event.
- ``whatsapp_conversations.state`` accepts the four booking states.

Downgrade drops all of it. WhatsApp bookings keep their rows (they are bookings) but lose channel, booker,
recipient and the encrypted condition; the conversation states are mapped back to ``idle`` first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0005_whatsapp_booking'
down_revision: Union[str, None] = '0004_whatsapp_foundations'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATES = "'new', 'consent_pending', 'declined', 'idle', 'human', 'emergency'"
NEW_STATES = OLD_STATES + ", 'booking_flow', 'awaiting_location', 'quoted', 'awaiting_payment'"


def upgrade() -> None:
    op.add_column('bookings', sa.Column('channel', sa.String(), server_default='app', nullable=False))
    op.add_column('bookings', sa.Column('booked_by_user_id', sa.UUID(), nullable=True))
    op.add_column('bookings', sa.Column('care_recipient_id', sa.UUID(), nullable=True))
    op.add_column('bookings', sa.Column('condition_encrypted', sa.Text(), nullable=True))
    op.add_column('bookings', sa.Column('needs_address_check', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.create_check_constraint('ck_bookings_channel', 'bookings', "channel IN ('app','whatsapp','admin')")
    op.create_foreign_key('fk_bookings_booked_by_user_id', 'bookings', 'users', ['booked_by_user_id'], ['id'])
    op.create_foreign_key('fk_bookings_care_recipient_id', 'bookings', 'care_recipients', ['care_recipient_id'], ['id'])
    op.create_index(op.f('ix_bookings_booked_by_user_id'), 'bookings', ['booked_by_user_id'], unique=False)
    op.create_index(op.f('ix_bookings_care_recipient_id'), 'bookings', ['care_recipient_id'], unique=False)

    op.create_table('quotes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(), server_default='draft', nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('sensitive_encrypted', sa.Text(), nullable=True),
    sa.Column('settings_id', sa.UUID(), nullable=True),
    sa.Column('total_kobo', sa.BigInteger(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('booking_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'submitted', 'out_of_area', 'quoted', 'confirmed', 'cancelled', 'expired', 'waitlist')", name='ck_quotes_status'),
    sa.CheckConstraint('total_kobo IS NULL OR total_kobo > 0', name='ck_quotes_total_positive'),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['whatsapp_contacts.id'], ),
    sa.ForeignKeyConstraint(['settings_id'], ['booking_settings.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('booking_id')
    )
    op.create_index('ix_quotes_contact_status', 'quotes', ['contact_id', 'status'], unique=False)

    op.create_table('booking_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('event', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('delivered_at', sa.DateTime(), nullable=True),
    sa.Column('claimed_at', sa.DateTime(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.String(), nullable=True),
    sa.CheckConstraint("event IN ('payment_received', 'payment_link_expiring', 'booking_cancelled_unpaid')", name='ck_booking_events_event'),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('booking_id', 'event', name='uq_booking_events_booking_event')
    )
    op.create_index('ix_booking_events_pending', 'booking_events', ['delivered_at', 'attempts'], unique=False)

    op.drop_constraint('ck_whatsapp_conversations_state', 'whatsapp_conversations', type_='check')
    op.create_check_constraint('ck_whatsapp_conversations_state', 'whatsapp_conversations', f"state IN ({NEW_STATES})")


def downgrade() -> None:
    op.drop_constraint('ck_whatsapp_conversations_state', 'whatsapp_conversations', type_='check')
    op.execute("UPDATE whatsapp_conversations SET state = 'idle', context = '{}'::jsonb "
               "WHERE state IN ('booking_flow', 'awaiting_location', 'quoted', 'awaiting_payment')")
    op.create_check_constraint('ck_whatsapp_conversations_state', 'whatsapp_conversations', f"state IN ({OLD_STATES})")

    op.drop_index('ix_booking_events_pending', table_name='booking_events')
    op.drop_table('booking_events')
    op.drop_index('ix_quotes_contact_status', table_name='quotes')
    op.drop_table('quotes')

    op.drop_index(op.f('ix_bookings_care_recipient_id'), table_name='bookings')
    op.drop_index(op.f('ix_bookings_booked_by_user_id'), table_name='bookings')
    op.drop_constraint('fk_bookings_care_recipient_id', 'bookings', type_='foreignkey')
    op.drop_constraint('fk_bookings_booked_by_user_id', 'bookings', type_='foreignkey')
    op.drop_constraint('ck_bookings_channel', 'bookings', type_='check')
    op.drop_column('bookings', 'needs_address_check')
    op.drop_column('bookings', 'condition_encrypted')
    op.drop_column('bookings', 'care_recipient_id')
    op.drop_column('bookings', 'booked_by_user_id')
    op.drop_column('bookings', 'channel')
