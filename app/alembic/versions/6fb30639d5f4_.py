"""empty message

Revision ID: 6fb30639d5f4
Revises: 1243ee6bcfa4, f1a2b3c4d5e6
Create Date: 2025-09-12 15:01:22.786017

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6fb30639d5f4'
down_revision: Union[str, None] = ('1243ee6bcfa4', 'f1a2b3c4d5e6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass