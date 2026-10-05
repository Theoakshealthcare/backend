"""Caregiver mobile support: push tokens, the en_route status and the visit code.

Revision ID: 0002_caregiver_mobile
Revises: 0001_baseline
Create Date: 2026-10-05

- ``push_tokens``: Expo push tokens, one row per device (token is unique).
- ``bookingstatus`` gains ``EN_ROUTE`` (Postgres stores the member NAME, like the other values).
- ``bookings``: the encrypted visit code and its wrong-attempt counter / lock timestamp.

Postgres cannot drop an enum value, so the downgrade first moves en_route bookings back to
ACCEPTED, then rebuilds the type without the value.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0002_caregiver_mobile'
down_revision: Union[str, None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('push_tokens',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token', sa.String(), nullable=False),
    sa.Column('platform', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("platform IN ('ios','android')", name='ck_push_tokens_platform'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token')
    )
    op.create_index(op.f('ix_push_tokens_user_id'), 'push_tokens', ['user_id'], unique=False)

    # Allowed inside a transaction on Postgres 12+; the new value just can't be used until commit.
    op.execute("ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'EN_ROUTE' AFTER 'ACCEPTED'")

    op.add_column('bookings', sa.Column('visit_code_enc', sa.String(), nullable=True))
    op.add_column('bookings', sa.Column('visit_code_failed_attempts', sa.Integer(), server_default='0', nullable=False))
    op.add_column('bookings', sa.Column('visit_code_locked_until', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'visit_code_locked_until')
    op.drop_column('bookings', 'visit_code_failed_attempts')
    op.drop_column('bookings', 'visit_code_enc')

    op.execute("UPDATE bookings SET status = 'ACCEPTED' WHERE status = 'EN_ROUTE'")
    op.execute("ALTER TYPE bookingstatus RENAME TO bookingstatus_old")
    op.execute(
        "CREATE TYPE bookingstatus AS ENUM "
        "('PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'DECLINED')"
    )
    op.execute(
        "ALTER TABLE bookings ALTER COLUMN status TYPE bookingstatus "
        "USING status::text::bookingstatus"
    )
    op.execute("DROP TYPE bookingstatus_old")

    op.drop_index(op.f('ix_push_tokens_user_id'), table_name='push_tokens')
    op.drop_table('push_tokens')
