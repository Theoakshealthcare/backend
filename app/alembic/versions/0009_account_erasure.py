"""Account erasure: a purge queue for provider files, and bookings that can lose their location.

Revision ID: 0009_account_erasure
Revises: 0007_whatsapp_notifications
Create Date: 2026-10-07

- ``pending_media_purges``: files at a media provider that could not be deleted when an account was erased.
- ``bookings.patient_address``, ``patient_latitude``, ``patient_longitude`` become nullable: erasing a patient
  clears the address and coordinates of their finished and cancelled bookings and keeps the money record.

MERGE NOTE: another open branch adds ``0008_dropout_fee_tiers`` with the same parent. Re-parent this file
(``down_revision = '0008_dropout_fee_tiers'``) after that branch merges.

Downgrade backfills erased bookings (address '[erased]', coordinates 0) and restores NOT NULL; the erased location cannot be recovered.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0009_account_erasure'
down_revision: Union[str, None] = '0007_whatsapp_notifications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCATION_COLUMNS = (("patient_address", sa.Text()), ("patient_latitude", sa.Float()), ("patient_longitude", sa.Float()))


def upgrade() -> None:
    op.create_table(
        'pending_media_purges',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('public_id', sa.String(), nullable=False),
        sa.Column('reason', sa.String(), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('purged_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_pending_media_purges_open', 'pending_media_purges', ['created_at'],
                    postgresql_where=sa.text('purged_at IS NULL'))
    for name, type_ in LOCATION_COLUMNS:
        op.alter_column('bookings', name, existing_type=type_, nullable=True)


def downgrade() -> None:
    # An erased booking has no location left to restore; fill placeholders so NOT NULL can come back.
    op.execute("UPDATE bookings SET patient_address = '[erased]' WHERE patient_address IS NULL")
    op.execute("UPDATE bookings SET patient_latitude = 0 WHERE patient_latitude IS NULL")
    op.execute("UPDATE bookings SET patient_longitude = 0 WHERE patient_longitude IS NULL")
    for name, type_ in LOCATION_COLUMNS:
        op.alter_column('bookings', name, existing_type=type_, nullable=False)
    op.drop_index('ix_pending_media_purges_open', table_name='pending_media_purges')
    op.drop_table('pending_media_purges')
