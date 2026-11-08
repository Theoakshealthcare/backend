"""Service catalogue: ``service_catalog``, one row per service type, seeded without prices.

Revision ID: 0026_service_catalog
Revises: 0025_visit_voice_notes
Create Date: 2026-11-08

Phase 2, slice 1. The table says which services are on offer, what they are called and how long a visit may be. It holds no
prices: those stay in ``booking_settings`` (base fee and per-service multipliers), and a service is bookable only when it is
enabled here and has a multiplier there.

Seed (no real data exists yet): nurse is enabled, the other four are not. Durations are stored in minutes (min 60, step 60,
max 1440); the application still validates whole hours until the booking-minutes slice lands.

Downgrade drops the table (the seeded rows and any admin edits are lost; nothing depended on it before this slice).
"""
import uuid
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0026_service_catalog'
down_revision: Union[str, None] = '0025_visit_voice_notes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (service_type, display_name, enabled, sort_order)
SEED = (
    ("nurse", "Nurse visit", True, 10),
    ("doctor", "Doctor visit", False, 20),
    ("physiotherapist", "Physiotherapy visit", False, 30),
    ("caregiver", "Caregiver visit", False, 40),
    ("nutritionist", "Nutrition consult", False, 50),
)


def upgrade() -> None:
    table = op.create_table(
        'service_catalog',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('service_type', sa.String(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('display_name', sa.String(), nullable=False),
        sa.Column('mode', sa.String(), nullable=False, server_default=sa.text("'home'")),
        sa.Column('min_minutes', sa.Integer(), nullable=False, server_default=sa.text('60')),
        sa.Column('step_minutes', sa.Integer(), nullable=False, server_default=sa.text('60')),
        sa.Column('max_minutes', sa.Integer(), nullable=False, server_default=sa.text('1440')),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('admin_users.id', ondelete='SET NULL'), nullable=True),
        sa.UniqueConstraint('service_type', name='uq_service_catalog_service_type'),
        sa.CheckConstraint(
            "service_type IN ('nurse','doctor','physiotherapist','caregiver','nutritionist')",
            name='ck_service_catalog_service_type'),
        sa.CheckConstraint("mode IN ('home','video','both')", name='ck_service_catalog_mode'),
        sa.CheckConstraint(
            'min_minutes > 0 AND step_minutes > 0 AND max_minutes >= min_minutes', name='ck_service_catalog_minutes'),
    )
    now = datetime.utcnow()
    op.bulk_insert(table, [
        {"id": uuid.uuid4(), "service_type": st, "enabled": enabled, "display_name": name, "mode": "home",
         "min_minutes": 60, "step_minutes": 60, "max_minutes": 1440, "sort_order": order,
         "created_at": now, "updated_at": now}
        for st, name, enabled, order in SEED
    ])


def downgrade() -> None:
    op.drop_table('service_catalog')
