"""merge heads

Revision ID: 8ae6d13a5a48
Revises: 20240709_add_fallguard_tables, 6800ac9d66f7
Create Date: 2025-07-21 17:49:30.551468

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8ae6d13a5a48'
down_revision: Union[str, None] = ('20240709_add_fallguard_tables', '6800ac9d66f7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass