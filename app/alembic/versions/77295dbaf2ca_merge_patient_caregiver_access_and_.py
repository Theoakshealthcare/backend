"""merge patient_caregiver_access and vitals/prescription heads

Revision ID: 77295dbaf2ca
Revises: 20240708b, 31c61f9fad8e
Create Date: 2025-07-06 18:18:42.115543

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77295dbaf2ca'
down_revision: Union[str, None] = ('20240708b', '31c61f9fad8e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass