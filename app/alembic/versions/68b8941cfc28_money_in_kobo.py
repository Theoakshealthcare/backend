"""money in kobo: convert naira Float/Numeric money columns to BIGINT kobo

Revision ID: 68b8941cfc28
Revises: 6fb30639d5f4
Create Date: 2026-10-05

Each column is converted in place (ALTER ... TYPE BIGINT USING ROUND(col::numeric * 100)) and
renamed to *_kobo. ``::numeric`` first matters for the Float columns: Postgres casts a double
to numeric at 15 significant digits, so 1531.885 becomes exactly 1531.885 (-> 153189 kobo)
rather than the binary 153188.49999999997 (-> 153188).

Downgrade divides by 100.0 and restores the original column type. It is lossless for
anything this revision produced, and for pre-revision values that were already 2dp.

Runs in one transaction (Alembic's default for Postgres DDL); the tables are small, but each
ALTER TYPE takes an ACCESS EXCLUSIVE lock, so run it in a quiet window.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "68b8941cfc28"
down_revision: Union[str, None] = "6fb30639d5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, old column, original type)
_COLUMNS = [
    ("bookings", "total_amount", sa.Float()),
    ("bookings", "service_fee", sa.Float()),
    ("bookings", "platform_fee", sa.Float()),
    ("booking_settings", "base_fee", sa.Float()),
    ("patient_wallets", "balance", sa.Numeric(10, 2)),
    ("patient_wallets", "total_deposited", sa.Numeric(10, 2)),
    ("patient_wallets", "total_spent", sa.Numeric(10, 2)),
    ("patient_wallets", "total_refunded", sa.Numeric(10, 2)),
    ("patient_transactions", "amount", sa.Numeric(10, 2)),
    ("patient_refund_requests", "amount", sa.Numeric(10, 2)),
    ("prescriptions", "total_cost", sa.Float()),
    ("prescription_settings", "base_fee", sa.Float()),
]


def upgrade() -> None:
    for table, col, old_type in _COLUMNS:
        op.alter_column(
            table, col,
            existing_type=old_type,
            type_=sa.BigInteger(),
            # a numeric server default cannot be cast to bigint automatically
            server_default=None,
            postgresql_using=f"ROUND({col}::numeric * 100)::bigint",
        )
        op.alter_column(table, col, new_column_name=f"{col}_kobo")


def downgrade() -> None:
    for table, col, old_type in _COLUMNS:
        op.alter_column(table, f"{col}_kobo", new_column_name=col)
        op.alter_column(
            table, col,
            existing_type=sa.BigInteger(),
            type_=old_type,
            postgresql_using=f"({col} / 100.0)::{old_type.compile(dialect=postgresql.dialect())}",
        )
