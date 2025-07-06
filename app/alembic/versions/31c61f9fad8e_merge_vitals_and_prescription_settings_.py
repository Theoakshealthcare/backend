"""merge vitals and prescription_settings heads

Revision ID: 31c61f9fad8e
Revises: 20240708, 40a74936a116
Create Date: 2025-07-06 17:32:57.825777

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '31c61f9fad8e'
down_revision: Union[str, None] = ('20240708', '40a74936a116')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass